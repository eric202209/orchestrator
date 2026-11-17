"""P42-V17-Z: authorized new paths no longer stand in for unseen implementation.

V17-Y named a new regression-test file as the only expected path.  Admission
returned ``authorized_new_paths_are_grounded`` and skipped Discovery, although
the required acceptance file exercised ``dotconf/parser.py`` through the
package re-export in ``dotconf/__init__.py``.  Planning never saw the parser
and proposed an ungrounded whole-file rewrite.

Admission now requires the existing implementation exercised by the required
acceptance file (direct imports plus one hop through package ``__init__``
facades, existing bound) to be completely materialized before new paths alone
count as sufficient grounding.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.planning import read_only_discovery
from app.services.orchestration.planning.direct_import_grounding import (
    MAX_DIRECT_IMPORT_FILES,
)
from app.services.orchestration.planning.read_only_discovery import (
    DISCOVERY_ADMISSION_REQUIRED,
    DISCOVERY_ADMISSION_SKIPPED,
    DiscoveryRequest,
    assess_discovery_admission,
    execute_discovery_request,
    materialize_observation_source_context,
    prepare_discovery_context,
    render_discovery_observation,
)
from app.services.orchestration.planning.source_materialization import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    acceptance_implementation_imports,
    materialize_planner_source_context,
    planner_expected_source_paths,
)
from app.task_intent import TaskIntentMode

# Verbatim persisted Task 309 description (sha256 5bdc18ff...).
TASK_309 = (
    "Fix a parsing defect in the dotconf configuration library. A quoted value "
    "that contains a `#` preceded by whitespace is cut off at that point instead "
    "of being kept as part of the value. For example, "
    '`loads(\'PASSWORD="abc # def"\')` raises `ParseError` ("unterminated '
    'double-quoted value") instead of returning `{"PASSWORD": "abc # def"}`, '
    'and `loads(dumps({"NOTE": "a #b"}))` does not round-trip. Single-quoted '
    "values are affected in the same way.\n\n"
    "Requirements:\n"
    "- Inside single-quoted and double-quoted values, `#` is ordinary text.\n"
    "- A quoted value may still be followed by whitespace and an inline "
    "`# comment`.\n"
    "- Preserve all other existing behavior, including inline comments on "
    "unquoted values and the existing `ParseError` messages.\n"
    "- Create a new regression test file `tests/test_quoted_hash_regression.py` "
    "containing at least one pytest test that fails on the current code and "
    "passes after the fix.\n"
    "- Do not modify `tests/test_dotconf_acceptance.py`.\n\n"
    "Independent acceptance contract (required):\n"
    "- `python -m pytest tests/test_dotconf_acceptance.py -q`"
)
REGRESSION = "tests/test_quoted_hash_regression.py"
DEFECT_MARKER = "def _parse_value"

# As in V17-Y, the defect lies beyond the 2000 B per-file Planning cap but
# inside Discovery's bounded read (4096 B, 200 lines).
_PARSER = (
    '"""Parse dotenv-style configuration text."""\n\n'
    "from .errors import ParseError\n\n"
    + "".join(
        f"def _helper_{index}(value):\n"
        f'    """Normalize value step {index} of the parsing pipeline."""\n'
        f"    return value\n\n\n"
        for index in range(24)
    )
    + "def loads(text, environ=None):\n    return {}\n\n\n"
    "def load(path, environ=None):\n    return {}\n\n\n"
    f"{DEFECT_MARKER}(raw, lineno):\n"
    "    return _strip_inline_comment(raw).strip()\n\n\n"
    "def _strip_inline_comment(raw):\n    return raw\n"
)


def _write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def dotconf(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "dotconf/__init__.py",
        "from .errors import ParseError\n"
        "from .parser import load, loads\n"
        "from .writer import dump, dumps\n",
    )
    _write(
        tmp_path,
        "dotconf/errors.py",
        "class ParseError(ValueError):\n    pass\n",
    )
    _write(tmp_path, "dotconf/parser.py", _PARSER)
    _write(
        tmp_path,
        "dotconf/writer.py",
        "def dumps(values):\n    return ''\n\n\ndef dump(values, path):\n    pass\n",
    )
    _write(tmp_path, "tests/test_parser.py", "from dotconf import loads\n")
    _write(
        tmp_path,
        "tests/test_dotconf_acceptance.py",
        "from dotconf import ParseError, dumps, loads\n\n\n"
        "def test_hash():\n"
        "    assert loads('PASSWORD=\"abc # def\"') == {'PASSWORD': 'abc # def'}\n",
    )
    return tmp_path


def _admission(root: Path, task: str, **kwargs):
    materialization = materialize_planner_source_context(
        root, task_description=task, supporting_paths=()
    )
    return materialization, assess_discovery_admission(
        prompt=task,
        planner_contract=None,
        materialization=materialization,
        acceptance_imports=acceptance_implementation_imports(root, task),
        **kwargs,
    )


def _prepare(root: Path, task: str, monkeypatch):
    stage_calls: list[tuple] = []
    events: list[dict] = []

    def fake_stage(**kwargs):
        stage_calls.append(kwargs["explicit_paths"])
        return read_only_discovery.DiscoveryObservation(
            action="read_file", status="completed", paths=(), content=""
        )

    monkeypatch.setattr(read_only_discovery, "run_discovery_stage", fake_stage)
    ctx = SimpleNamespace(
        prompt=task,
        planner_contract=None,
        orchestration_state=SimpleNamespace(project_dir=str(root)),
        emit_live=lambda *a, **k: None,
    )
    prepare_discovery_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=str,
        planner_service=None,
        emit_phase_event=lambda *a, **k: events.append(k.get("details") or {}),
        materialize=materialize_planner_source_context,
    )
    return events[-1], stage_calls


def test_v17y_layout_reproduces_the_preconditions(dotconf):
    assert len(_PARSER) > MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert planner_expected_source_paths(task_description=TASK_309) == (REGRESSION,)
    materialization = materialize_planner_source_context(
        dotconf, task_description=TASK_309, supporting_paths=()
    )
    paths = [item.relative_path for item in materialization.files]
    assert "dotconf/__init__.py" in paths
    assert "dotconf/parser.py" not in paths


def test_v17y_admits_discovery_for_unseen_reexported_implementation(dotconf):
    _, admission = _admission(dotconf, TASK_309)

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "acceptance_implementation_not_grounded"


def test_v17y_without_acceptance_facts_keeps_the_original_skip(dotconf):
    materialization = materialize_planner_source_context(
        dotconf, task_description=TASK_309, supporting_paths=()
    )
    admission = assess_discovery_admission(
        prompt=TASK_309, planner_contract=None, materialization=materialization
    )

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert admission.reason == "authorized_new_paths_are_grounded"


def test_prepare_discovery_context_runs_one_discovery_without_new_source(
    dotconf, monkeypatch
):
    before = materialize_planner_source_context(
        dotconf, task_description=TASK_309, supporting_paths=()
    )
    admission, stage_calls = _prepare(dotconf, TASK_309, monkeypatch)

    assert admission["discovery_admission"] == DISCOVERY_ADMISSION_REQUIRED
    assert admission["reason"] == "acceptance_implementation_not_grounded"
    assert stage_calls == [(REGRESSION,)]
    assert before.materialized_source_bytes <= before.maximum_total_source_bytes


def test_acceptance_facts_follow_one_facade_hop_only(dotconf):
    facts = acceptance_implementation_imports(dotconf, TASK_309)

    assert facts.resolved == (
        "dotconf/__init__.py",
        "dotconf/errors.py",
        "dotconf/parser.py",
        "dotconf/writer.py",
    )
    assert not facts.exceeded
    assert not any(path.startswith("tests/") for path in facts.resolved)


def test_discovery_read_exposes_the_defect_that_grounding_alone_does_not(dotconf):
    # Counterfactual: support-only grounding gets a head excerpt of the parser.
    grounded = materialize_planner_source_context(
        dotconf,
        task_description=TASK_309,
        supporting_paths=("dotconf/parser.py",),
    )
    parser = grounded.file_map()["dotconf/parser.py"]
    assert parser.truncated
    assert DEFECT_MARKER not in (parser.content or "")

    # Discovery's bounded read_file reaches it and is rendered for Planning.
    observation = execute_discovery_request(
        dotconf, DiscoveryRequest(action="read_file", path="dotconf/parser.py")
    )
    refreshed = materialize_observation_source_context(
        project_dir=dotconf,
        prompt=TASK_309,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
    )
    assert DEFECT_MARKER in render_discovery_observation(observation)
    assert refreshed.file_map()[REGRESSION].creation_authorized
    assert refreshed.materialized_source_bytes <= refreshed.maximum_total_source_bytes


def test_create_only_task_with_acceptance_of_new_module_keeps_skip(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "tests/test_slug_acceptance.py", "from pkg.slug import slug\n")
    task = (
        "Create `pkg/slug.py` with a `slug(text)` function.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_slug_acceptance.py -q`"
    )

    _, admission = _admission(tmp_path, task)

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert admission.reason == "authorized_new_paths_are_grounded"


def test_create_only_task_without_acceptance_keeps_skip(dotconf):
    task = "Create a new file `tests/test_extra.py` with one smoke test."

    _, admission = _admission(dotconf, task)

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert admission.reason == "authorized_new_paths_are_grounded"


def test_typed_create_only_intent_is_unchanged(dotconf):
    _, admission = _admission(
        dotconf, TASK_309, intent_mode=TaskIntentMode.CREATE_ONLY.value
    )

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert admission.reason == "typed_create_only_intent"


def test_complete_direct_implementation_with_new_test_keeps_skip(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/core.py", "def parse(text):\n    return text\n")
    _write(tmp_path, "tests/test_core_acceptance.py", "from pkg.core import parse\n")
    task = (
        "Fix `parse` and create a new regression test file "
        "`tests/test_core_regression.py`.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_core_acceptance.py -q`"
    )

    materialization, admission = _admission(tmp_path, task)

    assert materialization.file_map()["pkg/core.py"].truncated is False
    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert admission.reason == "authorized_new_paths_are_grounded"


def test_truncated_direct_implementation_with_new_test_requires_discovery(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/core.py", _PARSER)
    _write(tmp_path, "tests/test_core_acceptance.py", "from pkg.core import loads\n")
    task = (
        "Fix `loads` and create a new regression test file "
        "`tests/test_core_regression.py`.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_core_acceptance.py -q`"
    )

    _, admission = _admission(tmp_path, task)

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "acceptance_implementation_not_grounded"


def test_explicit_test_file_modification_is_unchanged(dotconf):
    task = (
        "Modify `tests/test_dotconf_acceptance.py` to add a test for comments.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_dotconf_acceptance.py -q`"
    )
    assert planner_expected_source_paths(task_description=task) == (
        "tests/test_dotconf_acceptance.py",
    )
    materialization, corrected = _admission(dotconf, task)
    original = assess_discovery_admission(
        prompt=task, planner_contract=None, materialization=materialization
    )

    assert corrected == original


def test_v17w_direct_import_shape_is_unchanged(tmp_path, monkeypatch):
    _write(tmp_path, "inventory/__init__.py")
    _write(tmp_path, "inventory/models.py", "class Item:\n    pass\n")
    _write(
        tmp_path,
        "inventory/service.py",
        "from inventory.models import Item\n\n\nclass Inventory:\n    pass\n",
    )
    _write(
        tmp_path,
        "tests/test_inventory_acceptance.py",
        "from inventory.service import Inventory\n",
    )
    task = (
        "Implement inventory reservation support.\n"
        "- Do not modify `tests/test_inventory_acceptance.py`.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_inventory_acceptance.py -q`"
    )
    materialization, corrected = _admission(tmp_path, task)
    original = assess_discovery_admission(
        prompt=task, planner_contract=None, materialization=materialization
    )

    assert corrected == original
    assert corrected.reason == "no_explicit_source_or_creation_path"
    assert [item.relative_path for item in materialization.files] == [
        "inventory/service.py",
        "tests/test_inventory_acceptance.py",
    ]


def test_cyclic_facade_terminates_and_stays_bounded(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "from .a import f\nfrom pkg import a\n")
    _write(
        tmp_path, "pkg/a.py", "import pkg\nfrom pkg.a import f\n\ndef f():\n    pass\n"
    )
    _write(tmp_path, "tests/test_acc.py", "from pkg import f\nimport pkg\n")
    task = (
        "Create a new file `tests/test_new.py`.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_acc.py -q`"
    )

    facts = acceptance_implementation_imports(tmp_path, task)

    assert facts.resolved == ("pkg/__init__.py", "pkg/a.py")
    assert not facts.exceeded


def test_unresolved_reexport_is_dropped_and_unbounded_facade_requires_discovery(
    tmp_path,
):
    modules = [f"m{index}" for index in range(MAX_DIRECT_IMPORT_FILES + 1)]
    _write(
        tmp_path,
        "pkg/__init__.py",
        "from .ghost import missing\n"
        + "".join(f"from .{name} import value_{name}\n" for name in modules),
    )
    for name in modules:
        _write(tmp_path, f"pkg/{name}.py", f"value_{name} = 1\n")
    _write(tmp_path, "tests/test_acc.py", "from pkg import value_m0\n")
    task = (
        "Create a new file `tests/test_new.py`.\n\n"
        "Independent acceptance contract (required):\n"
        "- `python -m pytest tests/test_acc.py -q`"
    )

    facts = acceptance_implementation_imports(tmp_path, task)
    _, admission = _admission(tmp_path, task)

    assert facts.exceeded
    assert len(facts.resolved) == MAX_DIRECT_IMPORT_FILES
    assert facts.unresolved == ()
    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "acceptance_implementation_not_grounded"
