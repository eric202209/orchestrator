"""Provider-free matrix for anchor-first ``replace_in_file`` old-text relocation.

The model owns *which* region to change; Orchestrator owns the exact current
bytes of that region.  These tests pin the one divergence that is deterministic
to repair — the file's own blank lines — and pin fail-closed behaviour for every
divergence that is not.
"""

from pathlib import Path

import pytest

from app.services.orchestration.planning.normalization import (
    normalize_blank_line_divergent_replace_anchors,
)
from app.services.orchestration.planning.operation_repair_anchors import (
    DERIVATION_BLANK_LINE_TOLERANT,
    DERIVATION_MINIMAL_DIVERGENT,
    derive_operation_anchors,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.planning.source_operation_verification import (
    verify_replace_in_file,
)


GROUPED_IMPORTS = (
    "import json\n"
    "import logging\n"
    "\n"
    "from sqlalchemy import func\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "\n"
    "def handler():\n"
    "    return logger\n"
)

# The model's reconstruction: every significant line verbatim, blank lines lost.
BLANK_LINE_DIVERGENT_OLD = (
    "import json\n"
    "import logging\n"
    "from sqlalchemy import func\n"
    "logger = logging.getLogger(__name__)"
)

BLANK_LINE_DIVERGENT_NEW = (
    "import json\n"
    "import logging\n"
    "from sqlalchemy import func\n"
    "from app.time_utils import utc_now\n"
    "logger = logging.getLogger(__name__)"
)


def _materialize(root: Path, *, path: str = "mod.py", source: str = GROUPED_IMPORTS):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description=f"Update the imports in {path}.",
        expected_paths=[path],
    )


def _plan(*ops: dict) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the requested bounded source change",
            "commands": [],
            "verification": "python -c 'import mod'",
            "rollback": None,
            "expected_files": ["mod.py"],
            "ops": list(ops),
        }
    ]


def _replace(path: str, old: str, new: str) -> dict:
    return {"op": "replace_in_file", "path": path, "old": old, "new": new}


def _normalize(root: Path, plan: list[dict], materialization):
    return normalize_blank_line_divergent_replace_anchors(
        plan, project_dir=root, source_materialization=materialization
    )


# --- B. zero semantic handles + unique deterministic legacy anchor ----------


def test_blank_line_divergent_anchor_is_realigned_onto_current_source(tmp_path):
    materialization = _materialize(tmp_path)
    plan = _plan(_replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW))

    # Before: the model's own anchor is rejected as stale.
    assert not verify_replace_in_file(
        materialization, "mod.py", BLANK_LINE_DIVERGENT_OLD, tmp_path
    ).verified

    normalized, report = _normalize(tmp_path, plan, materialization)

    assert report["changed"] is True
    assert report["reason"] == "blank_line_divergent_replace_anchor_realigned"
    assert report["normalized_anchors"][0]["path"] == "mod.py"
    assert (
        report["normalized_anchors"][0]["derivation"] == DERIVATION_BLANK_LINE_TOLERANT
    )

    injected = normalized[0]["ops"][0]["old"]
    # Orchestrator now owns the exact bytes, and they verify.
    assert injected in GROUPED_IMPORTS
    assert verify_replace_in_file(
        materialization, "mod.py", injected, tmp_path
    ).verified
    # No non-blank line outside the model's own `old` entered the anchor.
    assert [line for line in injected.splitlines() if line.strip()] == [
        line for line in BLANK_LINE_DIVERGENT_OLD.splitlines() if line.strip()
    ]
    # The model's semantic contribution is untouched.
    assert normalized[0]["ops"][0]["new"] == BLANK_LINE_DIVERGENT_NEW
    # Applying the plan leaves the rest of the file intact.
    applied = GROUPED_IMPORTS.replace(injected, BLANK_LINE_DIVERGENT_NEW, 1)
    assert "def handler():" in applied
    assert "from app.time_utils import utc_now" in applied


# --- the minimal-divergent derivation must never be injected first-path -----


def test_minimal_divergent_anchor_is_never_substituted_first_path(tmp_path):
    """Injecting a trimmed anchor while keeping the model's `new` would corrupt."""

    # Reproduces durable record log_entries.id=12820: the trailing lines of the
    # operation are unchanged, so the minimal-divergent derivation trims the
    # anchor down to the single import line it actually edits.
    source = (
        "from datetime import datetime\n\n\ndef utc_now():\n    return datetime.now()\n"
    )
    materialization = _materialize(tmp_path, source=source)
    old = "from datetime import datetime\ndef utc_now():\n    return datetime.now()"
    new = (
        "from datetime import datetime, timezone\n"
        "from app.time_utils import stamp\n"
        "def utc_now():\n"
        "    return datetime.now()"
    )

    anchors = derive_operation_anchors(
        step_number=1,
        operation_index=1,
        relative_path="mod.py",
        version_identity="v",
        original_old=old,
        original_new=new,
        full_source=source,
    )
    trimmed = [a for a in anchors if a.derivation == DERIVATION_MINIMAL_DIVERGENT]
    assert trimmed and trimmed[0].text == "from datetime import datetime"

    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, new)), materialization
    )
    # A blank-line-tolerant anchor exists here too, so the realignment fires --
    # but it must be the full region, never the trimmed minimal-divergent one.
    if report["changed"]:
        injected = normalized[0]["ops"][0]["old"]
        assert [line for line in injected.splitlines() if line.strip()] == [
            line for line in old.splitlines() if line.strip()
        ]
    else:
        assert normalized[0]["ops"][0]["old"] == old


def test_trimmed_minimal_divergent_only_case_fails_closed(tmp_path):
    """When only a trimmed anchor is derivable, nothing is injected."""

    source = "alpha = 1\nbeta = 2\ngamma = 3\n"
    materialization = _materialize(tmp_path, source=source)
    # `delta` is absent, so no realignment of the full region can succeed.
    old = "alpha = 1\ndelta = 9"
    new = "alpha = 1\ndelta = 10"
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, new)), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == old


# --- D. ambiguous anchor -----------------------------------------------------


def test_ambiguous_realignment_fails_closed(tmp_path):
    source = "def f():\n\n    return 1\n\n\ndef g():\n    pass\n\n\ndef f2():\n\n    return 1\n"
    materialization = _materialize(tmp_path, source=source)
    old = "    return 1"
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, "    return 2")), materialization
    )
    assert report["changed"] is False
    assert report["reason"] == "no_blank_line_divergent_replace_anchor"
    assert normalized[0]["ops"][0]["old"] == old


# --- E. zero match -----------------------------------------------------------


def test_absent_region_fails_closed(tmp_path):
    materialization = _materialize(tmp_path)
    old = "def never_written_here():\n    return 42"
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, "x")), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == old


def test_intra_line_whitespace_divergence_is_not_rescued(tmp_path):
    """Collapsed indentation is a fuzzy match, not a deterministic one."""

    materialization = _materialize(tmp_path)
    old = "def handler():\nreturn logger"  # de-indented body
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, "y")), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == old


def test_elided_interior_line_is_not_rescued(tmp_path):
    """A stitched, non-contiguous region is never realigned."""

    materialization = _materialize(tmp_path)
    old = "import json\nfrom sqlalchemy import func"  # `import logging` dropped
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, "z")), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == old


# --- F. wrong path -----------------------------------------------------------


def test_wrong_path_fails_closed(tmp_path):
    materialization = _materialize(tmp_path)
    normalized, report = _normalize(
        tmp_path,
        _plan(_replace("other.py", BLANK_LINE_DIVERGENT_OLD, "q")),
        materialization,
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["path"] == "other.py"


def test_path_outside_workspace_fails_closed(tmp_path):
    materialization = _materialize(tmp_path)
    normalized, report = _normalize(
        tmp_path,
        _plan(_replace("../escape.py", BLANK_LINE_DIVERGENT_OLD, "q")),
        materialization,
    )
    assert report["changed"] is False


# --- A/G. semantic target mode is untouched ----------------------------------


def test_semantic_replace_operations_are_never_touched(tmp_path):
    materialization = _materialize(tmp_path)
    op = {
        "op": "replace_in_file",
        "path": "mod.py",
        "target_id": "target-1",
        "new": "whatever",
    }
    normalized, report = _normalize(tmp_path, _plan(op), materialization)
    assert report["changed"] is False
    assert normalized[0]["ops"][0] == op


def test_mixed_replace_operations_are_never_touched(tmp_path):
    materialization = _materialize(tmp_path)
    op = {
        "op": "replace_in_file",
        "path": "mod.py",
        "target_id": "target-1",
        "old": BLANK_LINE_DIVERGENT_OLD,
        "new": "whatever",
    }
    normalized, report = _normalize(tmp_path, _plan(op), materialization)
    assert report["changed"] is False
    assert normalized[0]["ops"][0] == op


# --- E. no materialization ---------------------------------------------------


def test_unmaterialized_source_fails_closed(tmp_path):
    (tmp_path / "mod.py").write_text(GROUPED_IMPORTS, encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Create a brand new generated.py file.",
        expected_paths=["generated.py"],
    )
    normalized, report = _normalize(
        tmp_path,
        _plan(_replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW)),
        materialization,
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == BLANK_LINE_DIVERGENT_OLD


# --- M. source changed after materialization ---------------------------------


def test_source_version_change_fails_closed(tmp_path):
    materialization = _materialize(tmp_path)
    (tmp_path / "mod.py").write_text(
        GROUPED_IMPORTS + "\n# changed after materialization\n", encoding="utf-8"
    )
    normalized, report = _normalize(
        tmp_path,
        _plan(_replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW)),
        materialization,
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == BLANK_LINE_DIVERGENT_OLD


# --- in-plan buffer authority ------------------------------------------------


def test_path_already_mutated_earlier_in_plan_is_not_realigned(tmp_path):
    """Disk is not the authority once an earlier op in the same plan wrote it."""

    materialization = _materialize(tmp_path)
    plan = _plan(
        {"op": "write_file", "path": "mod.py", "content": "totally new content\n"},
        _replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW),
    )
    normalized, report = _normalize(tmp_path, plan, materialization)
    assert report["changed"] is False
    assert normalized[0]["ops"][1]["old"] == BLANK_LINE_DIVERGENT_OLD


def test_second_replace_of_same_path_is_not_realigned(tmp_path):
    materialization = _materialize(tmp_path)
    plan = _plan(
        _replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW),
        _replace("mod.py", "def handler():\nreturn logger", "other"),
    )
    normalized, report = _normalize(tmp_path, plan, materialization)
    assert len(report["normalized_anchors"]) == 1
    assert report["normalized_anchors"][0]["operation_index"] == 1
    assert normalized[0]["ops"][1]["old"] == "def handler():\nreturn logger"


# --- I/J. already-exact and non-replace operations ---------------------------


def test_exact_anchor_is_left_untouched(tmp_path):
    materialization = _materialize(tmp_path)
    old = "def handler():\n    return logger"
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", old, "    return None")), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == old


def test_new_file_write_is_unaffected(tmp_path):
    materialization = _materialize(tmp_path)
    op = {"op": "write_file", "path": "generated.py", "content": "print(1)\n"}
    normalized, report = _normalize(tmp_path, _plan(op), materialization)
    assert report["changed"] is False
    assert normalized[0]["ops"][0] == op


def test_empty_old_text_is_not_given_an_invented_anchor(tmp_path):
    materialization = _materialize(tmp_path)
    normalized, report = _normalize(
        tmp_path, _plan(_replace("mod.py", "", "content")), materialization
    )
    assert report["changed"] is False
    assert normalized[0]["ops"][0]["old"] == ""


# --- L. non-Python sources keep the same language-agnostic behaviour ---------


@pytest.mark.parametrize("path", ["notes.txt", "app.ts"])
def test_realignment_is_language_agnostic(tmp_path, path):
    source = "alpha\n\nbeta\n\ngamma\n"
    materialization = _materialize(tmp_path, path=path, source=source)
    normalized, report = _normalize(
        tmp_path,
        [
            {
                "step_number": 1,
                "description": "edit",
                "commands": [],
                "verification": "true",
                "rollback": None,
                "expected_files": [path],
                "ops": [_replace(path, "alpha\nbeta", "alpha\nbeta2")],
            }
        ],
        materialization,
    )
    assert report["changed"] is True
    assert normalized[0]["ops"][0]["old"] == "alpha\n\nbeta"


# --- plan shape preservation -------------------------------------------------


def test_non_dict_steps_and_missing_ops_are_preserved(tmp_path):
    materialization = _materialize(tmp_path)
    plan = [
        "not-a-step",
        {"step_number": 1, "description": "no ops", "commands": ["pytest"]},
    ]
    normalized, report = _normalize(tmp_path, plan, materialization)
    assert report["changed"] is False
    assert normalized[0] == "not-a-step"
    assert "ops" not in normalized[1]


# --- production wiring -------------------------------------------------------


def test_production_planning_flow_uses_the_realignment_helper():
    from app.services.orchestration.phases import planning_flow, planning_support

    assert (
        planning_flow._apply_replace_anchor_realignment
        is planning_support.apply_replace_anchor_realignment
    )
    source = Path(planning_flow.__file__).read_text(encoding="utf-8")
    # It must run before the whole-file escalation fallback, so an exact anchor
    # is preferred over rewriting the entire file.
    assert source.index("_apply_replace_anchor_realignment(ctx, sanitized_plan)") < (
        source.index("normalize_stale_replace_ops_to_small_file_writes(\n")
    )


def test_realignment_helper_emits_evidence_for_the_relocation(tmp_path):
    import logging
    from types import SimpleNamespace

    from app.services.orchestration.phases.planning_support import (
        apply_replace_anchor_realignment,
    )

    materialization = _materialize(tmp_path)
    emitted: list[dict] = []
    ctx = SimpleNamespace(
        logger=logging.getLogger("anchor-first-test"),
        planner_source_materialization=materialization,
        orchestration_state=SimpleNamespace(project_dir=tmp_path),
        emit_live=None,
    )
    plan = _plan(_replace("mod.py", BLANK_LINE_DIVERGENT_OLD, BLANK_LINE_DIVERGENT_NEW))

    def _capture(state, emit_live, **kwargs):
        emitted.append(kwargs)

    import app.services.orchestration.phases.planning_support as support

    original = support.emit_phase_event
    support.emit_phase_event = _capture
    try:
        result = apply_replace_anchor_realignment(ctx, plan)
    finally:
        support.emit_phase_event = original

    assert result[0]["ops"][0]["old"] in GROUPED_IMPORTS
    assert len(emitted) == 1
    assert emitted[0]["phase"] == "planning"
    assert emitted[0]["details"]["normalized_anchors"][0]["path"] == "mod.py"
