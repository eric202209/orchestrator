"""Provider-free OI-A bounded one-hop import-neighbor orientation regressions.

OI1-OI3 replay retained task texts against this repository's Git index; the
others build small tracked fixtures.  No provider, planner, or executor call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import app.services.orchestration.planning.repository_orientation as ro
from app.services.orchestration.planning.read_only_discovery import (
    MAX_ORIENTATION_BLOCK_BYTES,
    build_discovery_prompt,
)
from app.services.orchestration.planning.repository_orientation import (
    IMPORT_FACTS_HEADER,
    IMPORT_NEIGHBOR_BYTE_BUDGET,
    IMPORT_NEIGHBOR_FANIN_CAP,
    IMPORT_NEIGHBOR_MAX_HOPS,
    IMPORT_NEIGHBOR_RESERVED_SLOTS,
    ORIENTATION_BYTE_BUDGET,
    ORIENTATION_PATH_LIMIT,
    derive_repository_orientation,
    render_repository_orientation,
)
from app.tests.test_post33_pl24_orientation_informed_discovery import (
    TASK_222_TARGET,
    TASK_222_TEXT,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ROUTER = "app/api/v1/router.py"
PERMISSIONS = "app/api/v1/endpoints/permissions.py"

# Exact retained REENTRY-13 task text (discovery capture TASK block).
REENTRY13_TEXT = (
    "The permissions API is currently served under a doubled "
    "/api/v1/permissions/permissions/... prefix, so clients calling the public "
    "permission routes (pending, history, request, approve, deny, cleanup, "
    "check) under /api/v1/permissions/... cannot reach them. Make the "
    "permissions endpoints available at their public /api/v1/permissions/... "
    "routes without the doubled prefix, preserve existing permission behavior, "
    "and verify the public routes."
)
REENTRY11_TEXT = "Fix the doubled route in permissions endpoint."
FORBIDDEN_WORDING = (
    "parent",
    "likely fix",
    "edit this",
    "registration site",
    "composition owner",
    "recommended",
    "repair target",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _write(root: Path, relative: str, body: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    _git(tmp_path, "init", "-q")
    for relative, body in files.items():
        _write(tmp_path, relative, body)
    _git(tmp_path, "add", "-A")
    return tmp_path


def _oia(root: Path, task: str, **kwargs):
    return derive_repository_orientation(
        root, task, include_import_neighbors=True, **kwargs
    )


def _importers(orientation) -> list[str]:
    return [importer for importer, _ in orientation.import_neighbors]


def _require_repository_index() -> None:
    if not (REPOSITORY_ROOT / ".git").exists():
        pytest.skip("repository Git index unavailable")


# --- OI1-OI3: retained task texts on this repository -------------------------


def test_oi1_reentry13_router_surfaces_as_neutral_import_fact():
    _require_repository_index()
    before = derive_repository_orientation(REPOSITORY_ROOT, REENTRY13_TEXT)
    after = _oia(REPOSITORY_ROOT, REENTRY13_TEXT)

    assert ROUTER not in before.paths and not before.import_neighbors
    assert PERMISSIONS in before.paths and PERMISSIONS in after.paths
    assert (ROUTER, PERMISSIONS) in after.import_neighbors
    assert ROUTER not in after.paths

    rendered = render_repository_orientation(after)
    assert f"- {ROUTER} imports {PERMISSIONS}" in rendered
    lowered = rendered.lower()
    assert not any(word in lowered for word in FORBIDDEN_WORDING)
    # Topology only: no source text of the importer becomes visible.
    assert "include_router" not in rendered and "prefix=" not in rendered


def test_oi2_reentry11_lexical_router_is_preserved_not_duplicated():
    _require_repository_index()
    before = derive_repository_orientation(REPOSITORY_ROOT, REENTRY11_TEXT)
    after = _oia(REPOSITORY_ROOT, REENTRY11_TEXT)

    assert before.paths.index(ROUTER) == after.paths.index(ROUTER) == 4
    assert ROUTER not in _importers(after)


def test_oi3_same_file_control_target_stays_surfaced():
    _require_repository_index()
    after = _oia(REPOSITORY_ROOT, TASK_222_TEXT)
    assert TASK_222_TARGET in after.paths


# --- OI4-OI15: tracked fixtures ----------------------------------------------


def test_oi4_no_lexical_anchor_means_no_expansion(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/widget.py": "",
            "app/wiring.py": "from app import widget\n",
            "app/lonely.py": "",
        },
    )
    assert not _oia(root, "Explain the colour of the sky.").available
    plain = derive_repository_orientation(root, "Repair the lonely module.")
    oia = _oia(root, "Repair the lonely module.")
    assert oia.import_neighbors == ()
    assert render_repository_orientation(oia) == render_repository_orientation(plain)


def test_oi5_test_importers_and_test_anchors_are_not_expanded(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/widget.py": "",
            "app/tests/test_widget_usage.py": "from app import widget\n",
            "app/test_widget_helpers.py": "import app.widget\n",
            "app/wiring.py": "from app import widget\n",
            "app/tests/test_gadget.py": "",
            "app/gadget_user.py": "from app.tests import test_gadget\n",
        },
    )
    oia = _oia(root, "Repair widget and gadget behavior.")
    assert _importers(oia) == ["app/wiring.py"]


def test_oi6_untracked_importer_is_not_surfaced(tmp_path: Path):
    root = _project(tmp_path, {"app/widget.py": ""})
    _write(root, "app/wiring.py", "from app import widget\n")
    assert _oia(root, "Repair the widget.").import_neighbors == ()


def test_oi7_hub_over_fanin_cap_adds_no_subset(tmp_path: Path):
    files = {"app/widget.py": "", "app/gadget.py": ""}
    for index in range(IMPORT_NEIGHBOR_FANIN_CAP + 1):
        files[f"app/user_{index:02d}.py"] = "from app import widget\n"
    files["app/consumer.py"] = "import app.gadget\n"
    root = _project(tmp_path, files)
    oia = _oia(root, "Repair widget and gadget.")
    assert oia.import_anchors_suppressed_by_fanin == 1
    assert oia.import_neighbors == (("app/consumer.py", "app/gadget.py"),)


def test_oi8_expansion_is_exactly_one_hop(tmp_path: Path):
    assert IMPORT_NEIGHBOR_MAX_HOPS == 1
    root = _project(
        tmp_path,
        {
            "app/alpha.py": "",
            "app/bravo.py": "from app import alpha\n",
            "app/charlie.py": "from app import bravo\n",
        },
    )
    oia = _oia(root, "Repair alpha.")
    assert oia.paths == ("app/alpha.py",)
    assert oia.import_neighbors == (("app/bravo.py", "app/alpha.py"),)


def test_oi9_duplicate_importer_is_listed_once(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/widget_one.py": "",
            "app/widget_two.py": "",
            "app/hub.py": (
                "from app import widget_one\nimport app.widget_one\n"
                "from app.widget_two import thing\n"
            ),
        },
    )
    oia = _oia(root, "Repair widget modules.")
    assert _importers(oia) == ["app/hub.py"]
    assert oia.import_neighbors == (("app/hub.py", "app/widget_one.py"),)


def _crowded_project(tmp_path: Path, segment: str) -> Path:
    files = {}
    for index in range(ORIENTATION_PATH_LIMIT + 10):
        anchor = f"app/{segment}/widget_{index:02d}.py"
        files[anchor] = ""
        files[f"app/{segment}/zz/consumer_{index:02d}.py"] = (
            f"from app.{segment} import widget_{index:02d}\n"
        )
    return _project(tmp_path, files)


def test_oi10_rendered_bytes_stay_within_the_unchanged_budget(tmp_path: Path):
    root = _crowded_project(tmp_path, "a_rather_long_package_name_" + "x" * 40)
    oia = _oia(root, "Repair the widget.")
    assert oia.import_neighbors
    assert oia.bytes_used == oia.lexical_bytes_used + oia.import_neighbor_bytes_used
    assert oia.bytes_used <= ORIENTATION_BYTE_BUDGET == 3072
    assert oia.import_neighbor_bytes_used <= IMPORT_NEIGHBOR_BYTE_BUDGET
    listed = render_repository_orientation(oia).split("\n")
    path_lines = [line for line in listed if line.startswith("- ")]
    assert sum(len(f"{line}\n".encode()) for line in path_lines) == oia.bytes_used
    block = build_discovery_prompt("Repair the widget.", "", oia)
    assert block.endswith("END REPOSITORY ORIENTATION")
    assert (
        len(render_repository_orientation(oia).encode()) <= MAX_ORIENTATION_BLOCK_BYTES
    )


def test_oi11_path_cap_and_lexical_minimum_hold(tmp_path: Path):
    root = _crowded_project(tmp_path, "pkg")
    plain = derive_repository_orientation(root, "Repair the widget.")
    oia = _oia(root, "Repair the widget.")
    assert len(oia.paths) + len(oia.import_neighbors) <= ORIENTATION_PATH_LIMIT
    assert len(oia.import_neighbors) <= IMPORT_NEIGHBOR_RESERVED_SLOTS
    assert len(oia.paths) >= ORIENTATION_PATH_LIMIT - IMPORT_NEIGHBOR_RESERVED_SLOTS
    # Lexical results remain the unchanged derivation's own prefix.
    assert oia.paths == plain.paths[: len(oia.paths)]
    anchors = {anchor for _, anchor in oia.import_neighbors}
    assert anchors <= set(oia.paths)


def test_oi11_unused_reserved_slots_return_to_lexical(tmp_path: Path):
    root = _crowded_project(tmp_path, "pkg")
    # Only the first anchor has an importer, so three reserved slots are unused.
    for index in range(1, ORIENTATION_PATH_LIMIT + 10):
        _write(root, f"app/pkg/zz/consumer_{index:02d}.py", "")
    _git(root, "add", "-A")
    oia = _oia(root, "Repair the widget.")
    assert len(oia.import_neighbors) == 1
    assert len(oia.paths) == ORIENTATION_PATH_LIMIT - 1


def test_oi12_explicit_paths_keep_precedence(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/core/settings_loader.py": "",
            "app/widget.py": "",
            "app/wiring.py": "from app import widget\nfrom app.core import settings_loader\n",
        },
    )
    explicit = ("app/core/settings_loader.py",)
    plain = derive_repository_orientation(
        root, "Repair the widget.", explicit_paths=explicit
    )
    oia = _oia(root, "Repair the widget.", explicit_paths=explicit)
    assert oia.paths == plain.paths
    assert oia.paths[0] == "app/core/settings_loader.py"
    assert _importers(oia) == ["app/wiring.py"]


def test_oi13_unparseable_sources_fail_safe(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/widget.py": "",
            "app/broken_user.py": "from app import widget\ndef broken(:\n",
            "app/binary_user.py": "",
            "app/wiring.py": "from app import widget\n",
            "frontend/tsconfig.json": "{ not json",
            "frontend/src/widget.ts": "",
            "frontend/src/alias_user.ts": "import { w } from '@/widget';\n",
        },
    )
    (root / "app/binary_user.py").write_bytes(b"from app import widget\n\xff\xfe")
    oia = _oia(root, "Repair the widget.")
    assert _importers(oia) == ["app/wiring.py"]
    assert "app/widget.py" in oia.paths


def test_oi14_unsupported_import_expressions_are_ignored(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/widget.py": "",
            "app/dynamic_user.py": (
                "import importlib\nimportlib.import_module('app.widget')\n"
                "__import__('app.widget')\nname = 'app.widget'\n"
            ),
            "app/commented_user.py": "# from app import widget\n",
            "frontend/src/gadget.ts": "",
            "frontend/src/expr_user.ts": (
                "const name = './gadget';\nimport(name);\n"
                "// import { g } from './gadget';\n/* import './gadget'; */\n"
            ),
            "frontend/src/real_user.tsx": "import { g } from './gadget';\n",
        },
    )
    oia = _oia(root, "Repair widget and gadget.")
    assert _importers(oia) == ["frontend/src/real_user.tsx"]


def test_oi15_derivation_is_byte_identical_on_replay(tmp_path: Path):
    root = _crowded_project(tmp_path, "pkg")
    first = render_repository_orientation(_oia(root, "Repair the widget."))
    second = render_repository_orientation(_oia(root, "Repair the widget."))
    assert first == second and IMPORT_FACTS_HEADER in first


# --- resolution rules and integration boundaries ------------------------------


def test_python_relative_and_package_imports_resolve_conservatively(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "app/api/__init__.py": "",
            "app/api/endpoints/__init__.py": "from .widget import router\n",
            "app/api/endpoints/widget.py": "",
            "app/api/routes.py": "from app.api.endpoints import widget\n",
            "app/api/other.py": "from .endpoints import widget as w\n",
            "app/api/symbol_user.py": "from app.api.endpoints import something\n",
        },
    )
    oia = _oia(root, "Repair the widget.")
    assert sorted(_importers(oia)) == [
        "app/api/endpoints/__init__.py",
        "app/api/other.py",
        "app/api/routes.py",
    ]


def test_typescript_tsconfig_alias_and_lazy_import_resolve(tmp_path: Path):
    root = _project(
        tmp_path,
        {
            "frontend/tsconfig.json": (
                '{\n  "compilerOptions": {\n    /* aliases */\n'
                '    "paths": { "@/*": ["./src/*"] },\n  },\n}\n'
            ),
            "frontend/src/pages/Gizmo.tsx": "",
            "frontend/src/App.tsx": (
                "import { lazy } from 'react';\n"
                "const Gizmo = lazy(() => import('@/pages/Gizmo'));\n"
            ),
        },
    )
    oia = _oia(root, "The gizmo page is blank.")
    assert oia.import_neighbors == (
        ("frontend/src/App.tsx", "frontend/src/pages/Gizmo.tsx"),
    )


def test_default_derivation_and_provider_advisory_are_unchanged(tmp_path: Path):
    root = _project(
        tmp_path,
        {"app/widget.py": "", "app/wiring.py": "from app import widget\n"},
    )
    plain = derive_repository_orientation(root, "Repair the widget.")
    assert plain.import_neighbors == ()
    assert "orientation_import_neighbors_shown" not in plain.as_details()
    assert set(plain.as_provider_advisory()) == {*plain.as_details(), "paths"}
    oia = _oia(root, "Repair the widget.")
    assert oia.as_details()["orientation_import_neighbors_shown"] == 1
    assert "app/wiring.py" not in oia.as_provider_advisory()["paths"]


def test_discovery_stage_requests_import_neighbors(monkeypatch):
    import app.services.orchestration.planning.read_only_discovery as discovery

    seen = {}

    def _capture(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after orientation")

    monkeypatch.setattr(discovery, "derive_repository_orientation", _capture)

    class _State:
        project_dir = "."
        project_context = ""

    class _Ctx:
        read_only_discovery_completed = False
        runtime_service = object()
        orchestration_state = _State()
        prompt = "Repair the widget."

    with pytest.raises(RuntimeError, match="stop after orientation"):
        discovery.run_discovery_stage(
            ctx=_Ctx(),
            planning_timeout_seconds=1,
            extract_structured_text=str,
            planner_service=None,
            emit_phase_event=lambda *a, **k: None,
        )
    assert seen["include_import_neighbors"] is True


def test_oia_module_constants_stay_inside_the_existing_envelope():
    assert ro.ORIENTATION_PATH_LIMIT == 44 and ro.ORIENTATION_BYTE_BUDGET == 3072
    assert 4 <= IMPORT_NEIGHBOR_RESERVED_SLOTS <= 8
    assert IMPORT_NEIGHBOR_FANIN_CAP <= 10
    assert IMPORT_NEIGHBOR_BYTE_BUDGET < ORIENTATION_BYTE_BUDGET
