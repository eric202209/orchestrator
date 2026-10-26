"""Phase 40C-2 Correction C: bounded direct first-party import grounding.

Workflow C grounded ``dataflow/aggregate.py`` and claimed
``SKIPPED_SUFFICIENT_GROUNDING`` although the defect lived in
``dataflow/normalize.py``, which ``aggregate.py`` imports and Planning never
saw.  Admission now accounts for the direct first-party imports of the expected
source and materializes them through the existing bounded reader.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.planning import read_only_discovery
from app.services.orchestration.planning.direct_import_grounding import (
    DirectImportExpansion,
    direct_first_party_imports,
)
from app.services.orchestration.planning.read_only_discovery import (
    DISCOVERY_ADMISSION_REQUIRED,
    DISCOVERY_ADMISSION_SKIPPED,
    assess_discovery_admission,
    prepare_discovery_context,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)

TASK = (
    "dataflow/aggregate.py double counts customers. Fix the defect. "
    "Change only files under dataflow/."
)


def _write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def flat(tmp_path: Path) -> Path:
    _write(tmp_path, "dataflow/__init__.py")
    _write(
        tmp_path,
        "dataflow/aggregate.py",
        "import json\nimport os.path\nimport requests\n"
        "from collections import defaultdict\n"
        "from dataflow.normalize import normalize_records\n"
        "from dataflow.normalize import normalize_records as again\n"
        "from . import schema\n"
        "from .util import helper\n\n"
        "def total(records):\n    return len(normalize_records(records))\n",
    )
    _write(
        tmp_path,
        "dataflow/normalize.py",
        "from dataflow.aggregate import total\n\n"
        "def normalize_records(records):\n    return list(records)\n",
    )
    _write(tmp_path, "dataflow/schema.py", "FIELDS = ()\n")
    _write(tmp_path, "dataflow/util.py", "def helper():\n    return 1\n")
    _write(tmp_path, "tests/__init__.py")
    _write(
        tmp_path, "tests/test_aggregate.py", "from dataflow.aggregate import total\n"
    )
    return tmp_path


def test_direct_absolute_relative_and_duplicate_imports_resolve_once(flat):
    expansion = direct_first_party_imports(flat, ["dataflow/aggregate.py"])

    assert expansion.resolved == (
        "dataflow/normalize.py",
        "dataflow/schema.py",
        "dataflow/util.py",
    )
    assert expansion.unresolved == ()
    assert not expansion.exceeded


def test_standard_library_and_third_party_imports_are_ignored(flat):
    expansion = direct_first_party_imports(flat, ["dataflow/aggregate.py"])
    assert not any(
        name in path
        for path in expansion.resolved
        for name in ("json", "os", "requests", "collections")
    )


def test_circular_import_is_one_hop_and_never_reincludes_the_anchor(flat):
    expansion = direct_first_party_imports(
        flat, ["dataflow/aggregate.py", "dataflow/normalize.py"]
    )
    assert "dataflow/aggregate.py" not in expansion.resolved
    assert "dataflow/normalize.py" not in expansion.resolved
    # normalize.py's own imports are not followed beyond the anchors.
    only_normalize = direct_first_party_imports(flat, ["dataflow/normalize.py"])
    assert only_normalize.resolved == ("dataflow/aggregate.py",)


def test_src_layout_package_resolves(tmp_path):
    _write(tmp_path, "src/pkg/__init__.py")
    _write(tmp_path, "src/pkg/core.py", "from pkg.helpers import h\n")
    _write(tmp_path, "src/pkg/helpers.py", "def h():\n    return 0\n")
    expansion = direct_first_party_imports(tmp_path, ["src/pkg/core.py"])
    assert expansion.resolved == ("src/pkg/helpers.py",)


def test_package_symbol_import_resolves_submodule(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/a.py", "from pkg import b\n")
    _write(tmp_path, "pkg/b.py", "X = 1\n")
    expansion = direct_first_party_imports(tmp_path, ["pkg/a.py"])
    assert expansion.resolved == ("pkg/__init__.py", "pkg/b.py")


def test_missing_first_party_module_is_reported_unresolved(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/a.py", "import pkg.ghost\nfrom .phantom import x\n")
    expansion = direct_first_party_imports(tmp_path, ["pkg/a.py"])
    assert expansion.resolved == ()
    assert expansion.unresolved == ("pkg.ghost", ".phantom")


def test_relative_import_above_workspace_root_is_unresolved(tmp_path):
    _write(tmp_path, "mod.py", "from .. import outside\n")
    expansion = direct_first_party_imports(tmp_path, ["mod.py"])
    assert expansion.resolved == ()
    assert expansion.unresolved == ("..",)


def test_tests_and_non_python_anchors_are_not_expanded(flat):
    assert direct_first_party_imports(flat, ["tests/test_aggregate.py"]) == (
        DirectImportExpansion()
    )
    assert direct_first_party_imports(flat, ["README.md"]) == DirectImportExpansion()


def test_symlink_escape_is_not_followed(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "secret.py", "TOKEN = 1\n")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/a.py", "from pkg import secret\nfrom pkg import linkdir\n")
    os.symlink(outside / "secret.py", tmp_path / "pkg" / "secret.py")
    os.symlink(outside, tmp_path / "pkg" / "linkdir")
    expansion = direct_first_party_imports(tmp_path, ["pkg/a.py"])
    assert expansion.resolved == ("pkg/__init__.py",)


def test_symlinked_anchor_is_not_read(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    _write(outside, "evil.py", "from pkg import b\n")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/b.py")
    os.symlink(outside / "evil.py", tmp_path / "pkg" / "a.py")
    assert direct_first_party_imports(tmp_path, ["pkg/a.py"]).resolved == ()


def test_expansion_is_bounded(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    names = [f"m{index}" for index in range(6)]
    for name in names:
        _write(tmp_path, f"pkg/{name}.py", "X = 1\n")
    _write(tmp_path, "pkg/a.py", "".join(f"import pkg.{n}\n" for n in names))
    expansion = direct_first_party_imports(tmp_path, ["pkg/a.py"], maximum_files=4)
    assert len(expansion.resolved) == 4
    assert expansion.exceeded


def _materialize(root: Path, supporting=(), **kwargs):
    return materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=supporting, **kwargs
    )


def _admission(root: Path, materialization, imports):
    return assess_discovery_admission(
        prompt=TASK,
        planner_contract=None,
        materialization=materialization,
        direct_imports=imports,
    )


def test_red_mentioned_file_alone_used_to_claim_sufficiency(flat):
    # Without the dependency fact, admission is unchanged (historical result).
    admission = assess_discovery_admission(
        prompt=TASK, planner_contract=None, materialization=_materialize(flat)
    )
    assert admission.status == DISCOVERY_ADMISSION_SKIPPED


def test_missing_relevant_dependency_requires_discovery(flat):
    imports = direct_first_party_imports(flat, ["dataflow/aggregate.py"])
    admission = _admission(flat, _materialize(flat), imports)
    assert admission == read_only_discovery.DiscoveryAdmission(
        DISCOVERY_ADMISSION_REQUIRED, "direct_first_party_import_not_grounded"
    )


def test_grounded_dependencies_restore_sufficiency_with_provenance(flat):
    imports = direct_first_party_imports(flat, ["dataflow/aggregate.py"])
    materialization = _materialize(flat, supporting=imports.resolved)
    admission = _admission(flat, materialization, imports)
    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    record = next(
        item
        for item in materialization.files
        if item.relative_path == "dataflow/normalize.py"
    )
    assert record.content == (flat / "dataflow/normalize.py").read_text()
    assert record.version_identity and record.content_hash
    # A supporting record is source context, never a task-expected target.
    assert not record.expected


def test_budget_exhaustion_keeps_budget_and_requires_discovery(flat):
    imports = direct_first_party_imports(flat, ["dataflow/aggregate.py"])
    materialization = _materialize(
        flat, supporting=imports.resolved, maximum_total_source_bytes=400
    )
    assert sum(item.included_prompt_length for item in materialization.files) <= 400
    assert _admission(flat, materialization, imports).status == (
        DISCOVERY_ADMISSION_REQUIRED
    )


@pytest.mark.parametrize(
    ("imports", "reason"),
    [
        (
            DirectImportExpansion(unresolved=("pkg.ghost",)),
            "direct_first_party_import_unresolved",
        ),
        (DirectImportExpansion(exceeded=True), "direct_first_party_imports_unbounded"),
    ],
)
def test_unresolved_or_unbounded_imports_keep_discovery(flat, imports, reason):
    admission = _admission(flat, _materialize(flat), imports)
    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == reason


def test_prepare_discovery_context_materializes_direct_imports(flat):
    calls: list[tuple] = []

    def materialize(project_dir, **kwargs):
        calls.append(tuple(kwargs.get("supporting_paths") or ()))
        return materialize_planner_source_context(project_dir, **kwargs)

    events: list[dict] = []
    ctx = SimpleNamespace(
        prompt=TASK,
        planner_contract=None,
        orchestration_state=SimpleNamespace(project_dir=str(flat)),
        emit_live=lambda *a, **k: None,
    )

    prepare_discovery_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=str,
        planner_service=None,
        emit_phase_event=lambda *a, **k: events.append(k.get("details") or {}),
        materialize=materialize,
    )

    assert calls == [
        (),
        ("dataflow/normalize.py", "dataflow/schema.py", "dataflow/util.py"),
    ]
    assert events[-1]["discovery_admission"] == DISCOVERY_ADMISSION_SKIPPED
    paths = [item.relative_path for item in ctx.planner_source_materialization.files]
    assert "dataflow/normalize.py" in paths
