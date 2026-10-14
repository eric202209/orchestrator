"""Disposable AST-only fixtures; run with scripts/verification/run_root_tests.py.

The standalone runner forbids application imports and production state. When
collected by the full suite, the normal application conftest may have already
loaded ``app``; the non-execution assertion accounts for that pre-existing set.
"""

from __future__ import annotations

import ast
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts/verification/worker_root_verifier.py"
FIXTURE = REPO / "scripts/verification/fixtures/phase37-worker-safety-roots.1.json"
spec = importlib.util.spec_from_file_location("independent_root_verifier", TOOL)
v = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v
spec.loader.exec_module(v)


def write(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def add_module(root, module, text="", package=False):
    parts = module.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in v.NAMESPACE_LOCATIONS:
            path = root / (parent.replace(".", "/") + "/__init__.py")
            if not path.exists():
                write(root, str(path.relative_to(root)))
    suffix = "/__init__.py" if package else ".py"
    return write(root, module.replace(".", "/") + suffix, text)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    body = json.loads(FIXTURE.read_text())["body"]
    for records in body["roots"].values():
        for record in records:
            add_module(
                root, record["module"], package=record["path"].endswith("/__init__.py")
            )
    registrations = sorted(
        item["module"]
        for item in body["roots"]["registration"]
        if item["module"] != "app.celery_app"
    )
    write(
        root,
        "app/celery_app.py",
        "from celery import Celery\ncelery_app = Celery('synthetic', include="
        + repr(registrations)
        + ")\ncelery_app.autodiscover_tasks(['app.tasks'])\n",
    )
    add_module(
        root, "app.services.model_adaptation", "selected_profile = 1\n", package=True
    )
    for name in ("agent_runtime", "openclaw_service"):
        write(
            root,
            f"app/services/agents/{name}.py",
            "from app.services.model_adaptation import selected_profile\n",
        )
    policy = v.load_policy(
        FIXTURE,
        candidate_root=root,
        expected_digest=json.loads(FIXTURE.read_text())["digest"],
    )
    return root, policy


def manifest(derived):
    return {
        "source_root": derived.source_root,
        "policy_digest": derived.policy_digest,
        "records": copy.deepcopy(list(derived.records)),
    }


def verify(tree, omit=None):
    root, policy = tree
    derived = v.derive(root, policy)
    evidence = manifest(derived)
    if omit:
        evidence["records"] = [r for r in evidence["records"] if r["module"] != omit]
    return v.compare(derived, evidence)


def selected_policy(tmp_path, root, body):
    path = tmp_path / "independent-policy.json"
    envelope = {
        "body": body,
        "digest": v.body_digest(body),
        "issuer_envelope": {"kind": "isolated-test-fixture"},
    }
    path.write_text(json.dumps(envelope))
    return v.load_policy(path, candidate_root=root, expected_digest=envelope["digest"])


def planning_lazy_source(exports=("PlanCommitService", "PlanningSessionService")):
    export_map = {
        name: (f"app.services.planning.{name.lower()}", name) for name in exports
    }
    return (
        f"_EXPORTS = {export_map!r}\n"
        "def __getattr__(name):\n"
        "    module_name, attr_name = _EXPORTS[name]\n"
        "    from importlib import import_module\n"
        "    value = getattr(import_module(module_name), attr_name)\n"
        "    globals()[name] = value\n"
        "    return value\n"
    )


def reviewed_lazy_declaration(
    path, source, review_record="synthetic-independent-review-only"
):
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
    )
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_EXPORTS"
    )
    mapping = ast.literal_eval(assignment.value)
    export_map = v.lazy_export_map_entries(mapping)
    return {
        "module": "app.services.planning",
        "source_digest": v.digest(path.read_bytes()),
        "site_digest": v.digest(ast.dump(function, include_attributes=True).encode()),
        "export_map_digest": v.lazy_export_map_digest(export_map),
        "export_map": export_map,
        "export_names": [entry["name"] for entry in export_map],
        "loader_grammar": v.LAZY_LOADER_GRAMMAR,
        "verifier_compatibility_digest": v.COMPATIBILITY_DIGEST,
        "review_record_id": review_record,
        "classification": v.LAZY_CLASSIFICATION,
    }


def synthetic_planning_declaration(tmp_path, source=None):
    """Historical-shape declaration; the migrated fixture declares no lazy site."""
    source = source or planning_lazy_source()
    path = tmp_path / "synthetic-planning-declaration.py"
    path.write_text(source)
    return reviewed_lazy_declaration(path, source)


def stale_declaration_tree(tree, tmp_path):
    root, policy = tree
    body = policy.body
    body["lazy_imports"] = [
        synthetic_planning_declaration(tmp_path, planning_lazy_source(("Stale",)))
    ]
    return root, selected_policy(tmp_path, root, body)


def add_planning_lazy_package(
    root, exports=("PlanCommitService", "PlanningSessionService")
):
    path = add_module(
        root, "app.services.planning", planning_lazy_source(exports), package=True
    )
    for name in exports:
        add_module(
            root,
            f"app.services.planning.{name.lower()}",
            f"class {name}: pass\n",
        )
    return path


def reviewed_planning_tree(tree, tmp_path, source=None):
    root, policy = tree
    source = source or planning_lazy_source(("Export",))
    path = add_module(root, "app.services.planning", source, package=True)
    add_module(root, "app.services", "\n", package=True)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    add_module(root, "app.services.planning.export", "class Export: pass\n")
    body = policy.body
    body["lazy_imports"] = [reviewed_lazy_declaration(path, source)]
    return root, selected_policy(tmp_path, root, body), path


def test_l2_parent_initializer_removes_only_planning_reexports():
    module = ast.parse((REPO / "app/services/__init__.py").read_text())
    planning_imports = [
        node
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "planning"
    ]
    assert planning_imports == []
    all_assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    )
    exports = ast.literal_eval(all_assignment.value)
    assert "PlanCommitService" not in exports
    assert "PlanningSessionService" not in exports
    assert {"AgentRuntime", "TaskService", "PromptTemplates"} <= set(exports)


def test_l2_1_original_eager_parent_exports_are_detected(tree, tmp_path):
    root, _ = tree
    add_planning_lazy_package(root)
    add_module(
        root,
        "app.services",
        "from .planning import PlanCommitService, PlanningSessionService\n",
        package=True,
    )
    add_module(root, "app.tasks.worker", "import app.services\n")
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any(
        r.startswith("LAZY_REVIEW_REQUIRED:app.services.planning:")
        for r in result.reasons
    )
    stale = verify(stale_declaration_tree(tree, tmp_path))
    assert stale.root_status == "ROOT_INCONCLUSIVE"
    assert "LAZY_POLICY_IDENTITY_MISMATCH:app.services.planning" in stale.reasons
    assert "app.services.planning" in result.derivation.parsed_modules


def test_l2_2_corrected_parent_does_not_trigger_planning_lazy_exports(tree):
    root, _ = tree
    add_planning_lazy_package(root)
    add_module(root, "app.services", "\n", package=True)
    add_module(root, "app.tasks.worker", "import app.services\n")
    result = verify(tree)
    assert result.finite_gate_passed, result.reasons
    assert "app.services.planning" not in result.derivation.parsed_modules
    assert not any("app.services.planning" in reason for reason in result.reasons)


def test_l2_3_unrelated_worker_facing_eager_planning_export_reopens_hold(
    tree, tmp_path
):
    root, _ = tree
    add_planning_lazy_package(root, exports=("PlannerService",))
    add_module(
        root, "app.services", "from .planning import PlannerService\n", package=True
    )
    add_module(root, "app.tasks.worker", "import app.services\n")
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any(
        r.startswith("LAZY_REVIEW_REQUIRED:app.services.planning:")
        for r in result.reasons
    )
    stale = verify(stale_declaration_tree(tree, tmp_path))
    assert stale.root_status == "ROOT_INCONCLUSIVE"
    assert "LAZY_POLICY_IDENTITY_MISMATCH:app.services.planning" in stale.reasons


def test_l2_4_moving_eager_export_to_required_initializer_does_not_bypass(
    tree, tmp_path
):
    root, _ = tree
    add_planning_lazy_package(root)
    add_module(root, "app.services", "\n", package=True)
    add_module(
        root,
        "app.services.agents",
        "from app.services.planning import PlanCommitService\n",
        package=True,
    )
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any(
        r.startswith("LAZY_REVIEW_REQUIRED:app.services.planning:")
        for r in result.reasons
    )
    stale = verify(stale_declaration_tree(tree, tmp_path))
    assert stale.root_status == "ROOT_INCONCLUSIVE"
    assert "LAZY_POLICY_IDENTITY_MISMATCH:app.services.planning" in stale.reasons


def test_l2_5_computed_planning_dynamic_import_remains_inconclusive(tree):
    root, _ = tree
    add_module(
        root,
        "app.services.planning",
        "def __getattr__(name):\n"
        "    from importlib import import_module\n"
        "    target = 'app.services.planning.' + name\n"
        "    return getattr(import_module(target), name)\n",
        package=True,
    )
    add_module(
        root, "app.services", "from .planning import PlannerService\n", package=True
    )
    add_module(root, "app.tasks.worker", "import app.services\n")
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any(
        reason.startswith("UNRESOLVED_DYNAMIC_IMPORT:app.services.planning")
        for reason in result.reasons
    )


def test_l2_6_explicit_synthetic_planning_lazy_import_retains_semantics(tmp_path):
    package_root = tmp_path / "synthetic-l2"
    write(package_root, "synthetic_l2/__init__.py")
    write(package_root, "synthetic_l2/services/__init__.py")
    write(
        package_root,
        "synthetic_l2/services/planning/__init__.py",
        "_EXPORTS = {'Export': ('synthetic_l2.services.planning.leaf', 'Export')}\n"
        "def __getattr__(name):\n"
        "    from importlib import import_module\n"
        "    module_name, attr_name = _EXPORTS[name]\n"
        "    value = getattr(import_module(module_name), attr_name)\n"
        "    globals()[name] = value\n"
        "    return value\n",
    )
    write(
        package_root,
        "synthetic_l2/services/planning/leaf.py",
        "class Export:\n    marker = 'explicit-planning-import'\n",
    )
    write(
        package_root,
        "synthetic_l2/consumer.py",
        "from synthetic_l2.services.planning import Export\n",
    )
    sys.path.insert(0, str(package_root))
    try:
        consumer = importlib.import_module("synthetic_l2.consumer")
        assert consumer.Export.marker == "explicit-planning-import"
        assert "synthetic_l2.services.planning.leaf" in sys.modules
    finally:
        sys.path.remove(str(package_root))
        for name in list(sys.modules):
            if name == "synthetic_l2" or name.startswith("synthetic_l2."):
                del sys.modules[name]


def test_l2_7_candidate_controlled_mutation_cannot_suppress_required_identities(tree):
    root, policy = tree
    original = v.derive(root, policy).required
    add_module(
        root,
        "app.services.orchestration.worker_identity",
        "DEFAULT_AFFECTED_MODULES = ()\n"
        "def inspect_worker_provenance_boundary(*args, **kwargs):\n"
        "    raise RuntimeError('candidate checker must not execute')\n",
    )
    result = verify(tree, omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert result.derivation.required == original
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons


def test_l2_8_i12_model_adaptation_omission_still_fails_closed(tree):
    result = verify(tree, omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons
    assert {
        edge.source
        for edge in result.derivation.edges
        if edge.target == "app.services.model_adaptation" and edge.kind == "from"
    } == {
        "app.services.agents.agent_runtime",
        "app.services.agents.openclaw_service",
    }


@pytest.mark.parametrize("drift", ["source_root", "initializer", "manifest"])
def test_l2_9_source_initializer_and_manifest_drift_reject(tree, drift):
    root, _ = tree
    if drift == "initializer":
        (root / "app/services/agents/__init__.py").unlink()
        result = verify(tree)
        assert result.root_status == "ROOT_INVALID"
        assert not result.finite_gate_passed
        return
    result = verify(tree)
    evidence = manifest(result.derivation)
    if drift == "source_root":
        evidence["source_root"] = "/wrong-root"
    else:
        evidence["records"][0]["hash"] = "sha256:" + "0" * 64
    checked = v.compare(result.derivation, evidence)
    assert checked.root_status == "ROOT_VALID"
    assert checked.module_status == "MODULE_IDENTITY_INVALID"
    assert not checked.finite_gate_passed


def test_l2_10_correction_needs_no_lazy_exception(tree):
    root, policy = tree
    add_planning_lazy_package(root)
    add_module(root, "app.services", "\n", package=True)
    add_module(root, "app.tasks.worker", "import app.services\n")
    result = verify(tree)
    assert result.finite_gate_passed, result.reasons
    assert policy.body["lazy_imports"] == []
    assert not any("LAZY" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "fixture_id", [f"S{i}" for i in range(1, 13)], ids=[f"S{i}" for i in range(1, 13)]
)
def test_fixture(fixture_id, tree, tmp_path):
    root, policy = tree
    omitted = None
    if fixture_id == "S2":
        omitted = "app.services.model_adaptation"
    elif fixture_id == "S3":
        add_module(
            root,
            "app.services.orchestration.worker_identity",
            "DEFAULT_AFFECTED_MODULES = ()\ndef verify(): return 'VALID'\nraise RuntimeError('must never execute')\n",
        )
        omitted = "app.services.model_adaptation"
    elif fixture_id in ("S4", "S5"):
        source = (
            "app.services.session.session_lifecycle_service"
            if fixture_id == "S4"
            else "app.services.agents.openclaw_service"
        )
        add_module(root, source, "def deferred():\n    import app.new_direct\n")
        add_module(root, "app.new_direct")
        omitted = "app.new_direct"
    elif fixture_id == "S6":
        add_module(
            root, "app.services.agents", "from app.extra import x\n", package=True
        )
        add_module(root, "app.extra", "x=1\n")
        omitted = "app.extra"
    elif fixture_id == "S7":
        add_module(
            root,
            "app.tasks.worker",
            "from importlib import import_module as load\nload('app.literal')\n",
        )
        add_module(root, "app.literal")
        omitted = "app.literal"
    elif fixture_id == "S8":
        add_module(
            root,
            "app.tasks.worker",
            "import importlib\nimportlib.import_module(computed_name)\n",
        )
    elif fixture_id == "S9":
        add_module(root, "app.services.agents", package=False)
    elif fixture_id == "S10":
        add_module(
            root,
            "app.services.agents.runtime_adapters",
            "__path__.append('/other')\n",
            package=True,
        )
    elif fixture_id == "S11":
        (root / "app/tasks/worker.py").unlink()
    result = verify(tree, omitted)
    if fixture_id == "S12":
        evidence = manifest(result.derivation)
        evidence["policy_digest"] = "sha256:" + "0" * 64
        result = v.compare(result.derivation, evidence)
    if fixture_id == "S1":
        assert result.finite_gate_passed
        assert result.root_status == "ROOT_VALID"
        assert result.module_status == "MODULE_IDENTITY_VALID"
        assert policy.authorization_status == "TEST_ONLY_UNAUTHENTICATED"
    else:
        assert not result.finite_gate_passed
        assert result.root_status == (
            "ROOT_INCONCLUSIVE" if fixture_id == "S8" else "ROOT_INVALID"
        )
    if fixture_id in ("S2", "S3"):
        assert (
            "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons
        )
        origins = {
            e.source
            for e in result.derivation.edges
            if e.target == "app.services.model_adaptation" and e.kind == "from"
        }
        assert origins == {
            "app.services.agents.agent_runtime",
            "app.services.agents.openclaw_service",
        }
    if fixture_id == "S7":
        assert verify(tree).finite_gate_passed


def test_initializer_fixed_point_cycle_and_no_transitive_scan(tree):
    root, policy = tree
    add_module(root, "app.services.agents", "import app.loop_a\n", package=True)
    add_module(root, "app.loop_a", "import app.loop_b\n", package=True)
    add_module(root, "app.loop_b", "import app.loop_a\nimport app.leaf\n", package=True)
    add_module(
        root,
        "app.leaf",
        "import app.unbounded_missing\nraise RuntimeError('never execute')\n",
    )
    derived = v.derive(root, policy)
    assert derived.root_status == "ROOT_VALID"
    assert {"app.loop_a", "app.loop_b", "app.leaf"} <= set(derived.required)
    assert "app.unbounded_missing" not in derived.required
    assert "app.leaf" not in derived.parsed_modules
    assert len(derived.parsed_modules) == len(set(derived.parsed_modules))


@pytest.mark.parametrize(
    "limit,value",
    [
        ("explicit_roots", 1),
        ("derived_identities", 1),
        ("initializer_parses", 1),
        ("dotted_components", 1),
        ("source_bytes", 1),
        ("total_parsed_bytes", 1),
    ],
)
def test_limits_never_pass(tree, tmp_path, limit, value):
    root, policy = tree
    body = policy.body
    body["limits"][limit] = value
    reduced = selected_policy(tmp_path, root, body)
    result = verify((root, reduced))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert not result.finite_gate_passed
    assert any("LIMIT_EXCEEDED:" + limit in r for r in result.reasons)


@pytest.mark.parametrize(
    "statement",
    [
        "import importlib as il\nil.import_module('.literal', 'app')",
        "from importlib import import_module as load\nload('.literal', package='app')",
        "__import__('app.literal')",
        "import importlib\nimportlib.import_module(name='app.literal')",
        "from . import literal",
        "from .. import literal",
    ],
)
def test_literal_and_relative_resolution(tree, statement):
    root, policy = tree
    source = "app.tasks.worker"
    if statement == "from . import literal":
        target = "app.tasks.literal"
    else:
        target = "app.literal"
    add_module(root, target)
    add_module(root, source, statement + "\n")
    result = verify(tree)
    assert result.finite_gate_passed, result.reasons
    assert target in result.derivation.required


@pytest.mark.parametrize(
    "statement",
    [
        "from importlib import import_module as load\nload = other\nload('app.literal')",
        "import importlib\nimportlib.import_module(name)",
        "import importlib\nimportlib.import_module('.x', package)",
        "from importlib.machinery import SourceFileLoader as Loader\nLoader('x', 'x.py')",
        "from app.services.agents import *",
        "from .... import x",
        "exec(code)",
        "import sys\nsys.path.append('/writable')",
        "import sys\nsys.path = ['/writable']",
        "import importlib\nloader = importlib.import_module\nloader('app.literal')",
        "import importlib\ngetattr(importlib, 'import_module')('app.literal')",
    ],
)
def test_unknown_proof_is_inconclusive(tree, statement):
    root, _ = tree
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE", result.reasons
    assert not result.finite_gate_passed


def test_symbol_submodule_and_type_checking(tree):
    root, _ = tree
    add_module(root, "app.symbols", "VALUE=1\n", package=True)
    add_module(root, "app.symbols.child")
    add_module(
        root,
        "app.tasks.worker",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app.symbols import VALUE, child\n",
    )
    result = verify(tree)
    assert result.finite_gate_passed
    assert "app.symbols.child" in result.derivation.required
    assert "app.symbols.VALUE" not in result.derivation.required


def test_namespace_initializer_disappearance(tree):
    root, _ = tree
    (root / "app/services/agents/__init__.py").unlink()
    result = verify(tree)
    assert result.root_status == "ROOT_INVALID"
    assert not result.finite_gate_passed


def test_namespace_manifest_path_drift(tree):
    result = verify(tree)
    evidence = manifest(result.derivation)
    next(r for r in evidence["records"] if r["module"] == "app")["locations"].append(
        "/writable/app"
    )
    assert (
        v.compare(result.derivation, evidence).module_status
        == "MODULE_IDENTITY_INVALID"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "def __getattr__(name):\n    from importlib import import_module\n    return import_module(name)\n",
        "def __getattr__(name):\n    return exports[name]\n",
    ],
)
def test_lazy_without_independent_review(tree, statement):
    root, _ = tree
    add_module(root, "app.services.planning", statement, package=True)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("LAZY" in marker for marker in result.reasons)


def test_exact_reviewed_lazy_exception_and_new_worker_access(tree, tmp_path):
    root, policy = tree
    source = "_EXPORTS = {'Export': ('app.services.planning.leaf', 'Export')}\ndef __getattr__(name):\n    module_name, attr_name = _EXPORTS[name]\n    from importlib import import_module\n    value = getattr(import_module(module_name), attr_name)\n    return value\n"
    path = add_module(root, "app.services.planning", source, package=True)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    body = policy.body
    body["lazy_imports"] = [reviewed_lazy_declaration(path, source)]
    selected = selected_policy(tmp_path, root, body)
    assert verify((root, selected)).finite_gate_passed
    add_module(root, "app.tasks.worker", "from app.services.planning import Export\n")
    result = verify((root, selected))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert "WORKER_FACING_LAZY_ACCESS:app.services.planning" in result.reasons
    path.write_text(source + "# changed bytes\n")
    assert not verify((root, selected)).finite_gate_passed


@pytest.mark.parametrize("field", ["path", "hash", "kind"])
def test_module_identity_drift(tree, field):
    result = verify(tree)
    evidence = manifest(result.derivation)
    evidence["records"][0][field] = "wrong"
    check = v.compare(result.derivation, evidence)
    assert check.root_status == "ROOT_VALID"
    assert check.module_status == "MODULE_IDENTITY_INVALID"
    assert not check.finite_gate_passed


def test_duplicate_extra_and_root_mismatch(tree):
    result = verify(tree)
    evidence = manifest(result.derivation)
    evidence["records"].append(dict(evidence["records"][0]))
    assert not v.compare(result.derivation, evidence).finite_gate_passed
    evidence = manifest(result.derivation)
    evidence["records"].append(
        {"module": "app.fake", "path": "fake.py", "hash": "fake"}
    )
    comparison = v.compare(result.derivation, evidence)
    assert comparison.finite_gate_passed
    assert comparison.extra_records == ("app.fake",)
    assert "app.fake" not in comparison.derivation.required
    evidence["source_root"] = "/wrong"
    assert not v.compare(result.derivation, evidence).finite_gate_passed


@pytest.mark.parametrize(
    "change",
    [
        "smaller",
        "mapping",
        "duplicate",
        "namespace",
        "limit",
        "compatibility",
        "unknown",
        "revision",
        "digest",
    ],
)
def test_policy_rejections(tree, tmp_path, change):
    root, policy = tree
    body = policy.body
    if change == "smaller":
        body["roots"]["d37"].pop()
    elif change == "mapping":
        body["roots"]["d37"][0]["path"] = "../escape.py"
    elif change == "duplicate":
        body["roots"]["d37"].append(body["roots"]["d37"][0])
    elif change == "namespace":
        body["namespaces"][0]["locations"].append("other")
    elif change == "limit":
        body["limits"]["source_bytes"] *= 2
    elif change == "compatibility":
        body["verifier_compatibility_digest"] = "sha256:" + "0" * 64
    elif change == "unknown":
        body["unknown"] = True
    elif change == "revision":
        body["revision"] = True
    with pytest.raises(v.PolicyError):
        if change == "digest":
            v.load_policy(
                FIXTURE, candidate_root=root, expected_digest="sha256:" + "0" * 64
            )
        else:
            selected_policy(tmp_path, root, body)


def test_candidate_policy_cannot_replace_selected_policy(tree):
    root, policy = tree
    candidate_policy = root / "policy.json"
    candidate_policy.write_bytes(FIXTURE.read_bytes())
    with pytest.raises(v.PolicyError, match="CANDIDATE_POLICY_NOT_AUTHORITATIVE"):
        v.load_policy(
            candidate_policy, candidate_root=root, expected_digest=policy.digest
        )
    assert verify(tree).finite_gate_passed


def test_duplicate_json_keys_rejected(tree, tmp_path):
    root, policy = tree
    path = tmp_path / "duplicate.json"
    path.write_text('{"body": {}, "body": {}}')
    with pytest.raises(v.PolicyError, match="DUPLICATE_JSON_KEY"):
        v.load_policy(path, candidate_root=root, expected_digest=policy.digest)


@pytest.mark.parametrize(
    "case", ["symlink", "syntax", "decoding", "missing", "nonpackage"]
)
def test_source_failures(tree, tmp_path, case):
    root, _ = tree
    path = root / "app/tasks/worker.py"
    if case == "symlink":
        path.unlink()
        target = write(tmp_path, "outside.py")
        path.symlink_to(target)
    elif case == "syntax":
        path.write_text("def broken(\n")
    elif case == "decoding":
        path.write_bytes(b"\xff")
    elif case == "missing":
        path.write_text("import app.missing\n")
    elif case == "nonpackage":
        add_module(root, "app.impossible.child")
        (root / "app/impossible/__init__.py").unlink()
        add_module(root, "app.impossible")
        path.write_text("import app.impossible.child\n")
    result = verify(tree)
    assert not result.finite_gate_passed
    assert result.root_status == (
        "ROOT_INCONCLUSIVE" if case in ("syntax", "decoding") else "ROOT_INVALID"
    )


def test_deterministic_and_no_candidate_execution(tree):
    root, policy = tree
    add_module(
        root,
        "app.tasks.worker",
        "raise RuntimeError('must not execute')\nimport app.literal\n",
    )
    add_module(root, "app.literal", "raise RuntimeError('leaf must not execute')\n")
    existing_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    first = v.derive(root, policy)
    assert first == v.derive(root, policy)
    assert first.root_status == "ROOT_VALID"
    current_app_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    assert current_app_modules == existing_app_modules


@pytest.mark.parametrize(
    "statement, expect_access",
    [
        ("import app.services.planning as p\np.Export", True),
        ("import app.services.planning as p\nconsumer(p)", True),
        (
            "from importlib import import_module\np = import_module('app.services.planning')",
            False,
        ),
    ],
)
def test_reviewed_lazy_alias_access_holds(tree, tmp_path, statement, expect_access):
    root, policy = tree
    source = "_EXPORTS = {'Export': ('app.services.planning.leaf', 'Export')}\ndef __getattr__(name):\n    module_name, attr_name = _EXPORTS[name]\n    from importlib import import_module\n    value = getattr(import_module(module_name), attr_name)\n    return value\n"
    path = add_module(root, "app.services.planning", source, package=True)
    body = policy.body
    body["lazy_imports"] = [reviewed_lazy_declaration(path, source, "synthetic-only")]
    selected = selected_policy(tmp_path, root, body)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, selected))
    if expect_access:
        assert result.root_status == "ROOT_INCONCLUSIVE"
        assert any(
            marker in result.reasons
            for marker in (
                "WORKER_FACING_LAZY_ACCESS:app.services.planning",
                "PACKAGE_HANDLE_ESCAPE:app.tasks.worker:2",
            )
        )
    else:
        assert result.finite_gate_passed, result.reasons
        assert any(
            reason == "DORMANT_UNLOADED_LAZY_SITE:app.services.planning"
            for reason in result.derivation.diagnostics
        )


def test_builtin_import_shadow_is_not_proof(tree):
    root, _ = tree
    add_module(
        root,
        "app.tasks.worker",
        "def __import__(name): return other(name)\n__import__('app.fake')\n",
    )
    result = verify(tree)
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("AMBIGUOUS_DYNAMIC_ALIAS" in r for r in result.reasons)


def test_unreviewed_mechanism_cannot_use_review_label(tree, tmp_path):
    root, policy = tree
    source = "def __getattr__(name):\n    from importlib import import_module\n    return import_module(name)\n"
    path = add_module(root, "app.services.planning", source, package=True)
    body = policy.body
    declaration = synthetic_planning_declaration(tmp_path)
    declaration["source_digest"] = v.digest(path.read_bytes())
    declaration["site_digest"] = v.digest(
        ast.dump(ast.parse(source).body[0], include_attributes=True).encode()
    )
    body["lazy_imports"] = [declaration]
    selected = selected_policy(tmp_path, root, body)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    assert not verify((root, selected)).finite_gate_passed


def test_candidate_policy_symlink_not_authority(tree):
    root, policy = tree
    alias = root / "policy.json"
    alias.symlink_to(FIXTURE)
    with pytest.raises(v.PolicyError, match="CANDIDATE_POLICY_NOT_AUTHORITATIVE"):
        v.load_policy(alias, candidate_root=root, expected_digest=policy.digest)


def test_candidate_mutations_leave_required_set_unchanged(tree):
    root, policy = tree
    original = v.derive(root, policy).required
    add_module(
        root,
        "app.services.orchestration.worker_identity",
        "DEFAULT_AFFECTED_MODULES = ()\ndef inspect_worker_provenance_boundary(*a): return 'VALID'\n",
    )
    write(root, "policy.json", "{}")
    assert v.derive(root, policy).required == original


def test_cli_requires_both_verdicts_and_never_claims_production(
    tree, tmp_path, monkeypatch, capsys
):
    root, policy = tree
    base = [
        str(TOOL),
        "--candidate-root",
        str(root),
        "--trusted-policy",
        str(FIXTURE),
        "--policy-digest",
        policy.digest,
    ]
    monkeypatch.setattr(sys, "argv", base)
    assert v.main() == 2
    output = json.loads(capsys.readouterr().out)
    assert not output["finite_gate_passed"]
    assert output["module_status"] == "MODULE_IDENTITY_INCONCLUSIVE"
    evidence = manifest(v.derive(root, policy))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(evidence))
    monkeypatch.setattr(sys, "argv", base + ["--manifest", str(path)])
    assert v.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["finite_gate_passed"]
    assert output["production_acceptance"] is False
    evidence["records"] = [
        r for r in evidence["records"] if r["module"] != "app.services.model_adaptation"
    ]
    path.write_text(json.dumps(evidence))
    assert v.main() == 2
    output = json.loads(capsys.readouterr().out)
    assert output["root_status"] == "ROOT_INVALID"
    assert not output["finite_gate_passed"]


def test_sc1_01_exact_reviewed_dormant_site(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    result = verify((root, policy))
    assert result.finite_gate_passed, result.reasons
    assert result.root_status == "ROOT_VALID"
    assert (
        result.derivation.dormant_lazy_sites[0]["classification"]
        == v.LAZY_CLASSIFICATION
    )
    assert (
        "DORMANT_UNLOADED_LAZY_SITE:app.services.planning"
        in result.derivation.diagnostics
    )
    assert not any("LAZY" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "case, statement, reason",
    [
        (
            "SC1-02",
            "from app.services.planning import Export",
            "WORKER_FACING_LAZY_ACCESS",
        ),
        (
            "SC1-03",
            "import app.services.planning as p\np.Export",
            "WORKER_FACING_LAZY_ACCESS",
        ),
        (
            "SC1-04",
            "import app.services.planning as p\ngetattr(p, dynamic_name)",
            "WORKER_FACING_LAZY_ACCESS",
        ),
        (
            "SC1-05",
            "import app.services.planning as p\nhasattr(p, 'Export')",
            "WORKER_FACING_LAZY_ACCESS",
        ),
        (
            "SC1-05-direct",
            "import app.services.planning as p\np.__getattr__('Export')",
            "WORKER_FACING_LAZY_ACCESS",
        ),
        ("SC1-06", "from app.services.planning import *", "WILDCARD_IMPORT"),
        (
            "SC1-13",
            "from app.services.planning import Export",
            "WORKER_FACING_LAZY_ACCESS",
        ),
    ],
)
def test_sc1_lazy_access_and_unsupported_cases(tree, tmp_path, case, statement, reason):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert not result.finite_gate_passed, case
    assert any(reason in item for item in result.reasons), result.reasons


@pytest.mark.parametrize(
    "statement, child",
    [
        ("import app.services.planning", None),
        ("import app.services.planning.export", "app.services.planning.export"),
        ("from app.services.planning import export", "app.services.planning.export"),
        (
            "from importlib import import_module\nimport_module('app.services.planning')",
            None,
        ),
        (
            "from importlib import import_module\nimport_module('app.services.planning.export')",
            "app.services.planning.export",
        ),
    ],
)
def test_sc1_07_literal_imports_are_not_lazy_attribute_access(
    tree, tmp_path, statement, child
):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    if child:
        add_module(root, child, "class Export: pass\n")
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.finite_gate_passed, result.reasons
    assert "WORKER_FACING_LAZY_ACCESS:app.services.planning" not in result.reasons


def test_sc1_08_computed_importlib_target_holds(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(
        root,
        "app.tasks.worker",
        "import importlib\nimportlib.import_module(computed_target)\n",
    )
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("UNRESOLVED_DYNAMIC_IMPORT" in reason for reason in result.reasons)


def test_unknown_fromlist_holds(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", "from app.services.planning import Unknown\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("UNRESOLVED_FROM_IMPORT" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "mutation",
    [
        "_EXPORTS.update({'Export': ('app.other', 'Export')})",
        "_EXPORTS.clear()",
        "_EXPORTS.pop('Export')",
        "_EXPORTS.popitem()",
        "_EXPORTS.setdefault('Export', ('app.other', 'Export'))",
        "_EXPORTS |= {'Export': ('app.other', 'Export')}",
        "_EXPORTS.rotate()",
        "_EXPORTS['Export'] = ('app.other', 'Export')",
        "del _EXPORTS['Export']",
        "alias = _EXPORTS\nalias.clear()",
        "container = [_EXPORTS]",
        "unknown_helper(_EXPORTS)",
        "def return_map():\n    return _EXPORTS",
        "_EXPORTS = {}",
        "first, second = _EXPORTS",
    ],
)
def test_sc1_09_export_map_mutations_and_escapes_hold(tree, tmp_path, mutation):
    source = planning_lazy_source(("Export",)) + "\n" + mutation + "\n"
    root, policy, _ = reviewed_planning_tree(tree, tmp_path, source)
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", mutation
    assert any(
        marker in reason
        for reason in result.reasons
        for marker in ("EXPORT_MAP", "LAZY_MECHANISM", "LAZY_POLICY")
    ), result.reasons


def test_sc1_10_computed_export_map_entry_holds(tree, tmp_path):
    source = (
        "module_name = 'app.services.planning.export'\n"
        "_EXPORTS = {'Export': (module_name, 'Export')}\n"
        "def __getattr__(name):\n"
        "    module_name, attr_name = _EXPORTS[name]\n"
        "    from importlib import import_module\n"
        "    value = getattr(import_module(module_name), attr_name)\n"
        "    return value\n"
    )
    root, base_policy = tree
    path = add_module(root, "app.services.planning", source, package=True)
    add_module(root, "app.services", "\n", package=True)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    add_module(root, "app.services.planning.export", "class Export: pass\n")
    body = base_policy.body
    declaration = synthetic_planning_declaration(tmp_path)
    function = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
    )
    declaration["source_digest"] = v.digest(path.read_bytes())
    declaration["site_digest"] = v.digest(
        ast.dump(function, include_attributes=True).encode()
    )
    body["lazy_imports"] = [declaration]
    policy = selected_policy(tmp_path, root, body)
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("LAZY_MECHANISM_UNSUPPORTED" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "statement",
    [
        "import app.services.planning as p\nq = p\nq.Export",
        "import app.services.planning as p\ncontainer = [p]",
        "import app.services.planning as p\nhelper(p)",
        "import app.services.planning as p\ndef return_package():\n    return p",
        "import app.services.planning as p\np = other",
        "import app.services.planning as p\ndef helper(p):\n    return p.Export",
    ],
)
def test_sc1_11_alias_assignment_rebinding_and_escape_hold(tree, tmp_path, statement):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", result.reasons
    assert any("PACKAGE_" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "statement",
    [
        "import app.services.planning as p\nglobals()['p'].Export",
        "import app.services.planning as p\nlocals()['p'].Export",
        "import app.services.planning as p\nvars(p).get('Export')",
        "import app.services.planning as p\np.__dict__['Export']",
        "import app.services.planning as p\nunknown_helper(p)",
    ],
)
def test_sc1_12_reflection_and_helper_access_hold(tree, tmp_path, statement):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", result.reasons
    assert any(
        "REFLECTION_UNSUPPORTED" in reason or "PACKAGE_HANDLE_ESCAPE" in reason
        for reason in result.reasons
    )


def test_sc1_14_candidate_policy_substitution_is_rejected(tree):
    root, policy = tree
    candidate_policy = root / "candidate-policy.json"
    candidate_policy.write_bytes(FIXTURE.read_bytes())
    with pytest.raises(v.PolicyError, match="CANDIDATE_POLICY_NOT_AUTHORITATIVE"):
        v.load_policy(
            candidate_policy, candidate_root=root, expected_digest=policy.digest
        )


def test_sc1_15_candidate_checker_substitution_is_never_executed(tree):
    root, policy = tree
    add_module(
        root,
        "app.services.orchestration.worker_identity",
        "raise RuntimeError('candidate checker executed')\n",
    )
    result = verify((root, policy), omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons


def test_sc1_16_i12_model_adaptation_omission_remains_invalid(tree):
    result = verify(tree, omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ("_EXPORTS.update({'Export': ('app.other', 'Export')})", "EXPORT_MAP"),
        ("_EXPORTS.clear()", "EXPORT_MAP"),
        ("_EXPORTS.pop('Export')", "EXPORT_MAP"),
        ("_EXPORTS.setdefault('Export', ('app.other', 'Export'))", "EXPORT_MAP"),
        ("_EXPORTS['Export'] = ('app.other', 'Export')", "EXPORT_MAP"),
        ("del _EXPORTS['Export']", "EXPORT_MAP"),
        ("alias = _EXPORTS\nalias['Export'] = ('app.other', 'Export')", "EXPORT_MAP"),
        ("box = {'exports': _EXPORTS}", "EXPORT_MAP"),
        ("send(_EXPORTS)", "EXPORT_MAP"),
        ("def get_map():\n    return _EXPORTS", "EXPORT_MAP"),
        ("_EXPORTS = {}", "EXPORT_MAP"),
        ("a, b = _EXPORTS", "EXPORT_MAP"),
        ("globals()['x'] = _EXPORTS", "EXPORT_MAP"),
        ("locals()['x'] = _EXPORTS", "EXPORT_MAP"),
        ("vars()['x'] = _EXPORTS", "EXPORT_MAP"),
        ("module.__dict__['_EXPORTS'] = {}", "REFLECTION"),
    ],
)
def test_additional_export_map_mutation_negatives(tree, tmp_path, mutation, reason):
    source = planning_lazy_source(("Export",)) + "\n" + mutation + "\n"
    root, policy, _ = reviewed_planning_tree(tree, tmp_path, source)
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", result.reasons
    assert any(reason in item for item in result.reasons), result.reasons


def test_changed_lazy_source_site_and_map_identities_hold(tree, tmp_path):
    root, policy, path = reviewed_planning_tree(tree, tmp_path)
    body = policy.body
    body["lazy_imports"][0]["source_digest"] = "sha256:" + "0" * 64
    changed = selected_policy(tmp_path, root, body)
    result = verify((root, changed))
    assert any("LAZY_POLICY_IDENTITY_MISMATCH" in reason for reason in result.reasons)
    assert path.exists()


def test_changed_lazy_site_digest_holds(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    body = policy.body
    body["lazy_imports"][0]["site_digest"] = "sha256:" + "0" * 64
    selected = selected_policy(tmp_path, root, body)
    result = verify((root, selected))
    assert any("LAZY_POLICY_IDENTITY_MISMATCH" in reason for reason in result.reasons)


def test_changed_lazy_map_digest_is_rejected(tree, tmp_path):
    root, policy = tree
    body = policy.body
    body["lazy_imports"] = [synthetic_planning_declaration(tmp_path)]
    body["lazy_imports"][0]["export_map_digest"] = "sha256:" + "0" * 64
    with pytest.raises(v.PolicyError, match="LAZY_EXPORT_MAP_IDENTITY"):
        selected_policy(tmp_path, root, body)


@pytest.mark.parametrize(
    "change, expected",
    [
        ("policy_digest", "POLICY_DIGEST_MISMATCH"),
        ("verifier_compatibility_digest", "POLICY_VERSION_COMPATIBILITY"),
        ("duplicate_lazy", "DUPLICATE_LAZY_SITE"),
        ("schema", "POLICY_VERSION_COMPATIBILITY"),
    ],
)
def test_policy_digest_compatibility_duplicate_and_schema_negatives(
    tree, tmp_path, change, expected
):
    root, policy = tree
    body = policy.body
    if change == "policy_digest":
        with pytest.raises(v.PolicyError, match="POLICY_DIGEST_MISMATCH"):
            v.load_policy(
                FIXTURE, candidate_root=root, expected_digest="sha256:" + "0" * 64
            )
        return
    if change == "verifier_compatibility_digest":
        body["verifier_compatibility_digest"] = "sha256:" + "0" * 64
    elif change == "duplicate_lazy":
        body["lazy_imports"] = [synthetic_planning_declaration(tmp_path)] * 2
    else:
        body["schema"] = "orchestrator.worker-root-policy.v1"
    with pytest.raises(v.PolicyError, match=expected):
        selected_policy(tmp_path, root, body)


def test_unknown_loader_and_parent_initializer_edge_hold(tree, tmp_path):
    source = (
        "_EXPORTS = {'Export': ('app.services.planning.export', 'Export')}\n"
        "def __getattr__(name):\n"
        "    module_name, attr_name = _EXPORTS[name]\n"
        "    from importlib import import_module as load\n"
        "    value = getattr(load(module_name), attr_name)\n"
        "    return value\n"
    )
    root, policy, _ = reviewed_planning_tree(tree, tmp_path, source)
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert any("LAZY_MECHANISM_UNSUPPORTED" in reason for reason in result.reasons)
    add_module(
        root,
        "app.services.agents",
        "from app.services.planning import Export\n",
        package=True,
    )
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"


PLANNING_ALIAS = "import app.services.planning as p\n"
C1_REASON = "LAZY_GETATTR_CALLABLE_ACCESS:app.tasks.worker:"


@pytest.mark.parametrize(
    "case, statement, line",
    [
        ("C1-01", "p.__getattr__('Export')", 2),
        ("C1-02", "fn = p.__getattr__\nfn('Export')", 2),
        ("C1-03", "fn = p.__getattr__", 2),
        ("C1-04", "box = [p.__getattr__]", 2),
        ("C1-05", "box = {'loader': p.__getattr__}", 2),
        ("C1-06", "helper(p.__getattr__)", 2),
        ("C1-07", "def leak():\n    return p.__getattr__", 3),
        (
            "C1-08",
            "def outer():\n    fn = p.__getattr__\n"
            "    def inner():\n        return fn('Export')\n    return inner",
            3,
        ),
        ("C1-08-lambda", "leak = lambda: p.__getattr__", 2),
        ("C1-09", "if flag:\n    fn = p.__getattr__", 3),
        ("C1-10", "def never_called():\n    p.__getattr__", 3),
        ("C1-11", "p.__getattr__.__call__('Export')", 2),
        ("C1-12", "getattr(p, '__getattr__')", 2),
        ("C1-store", "p.__getattr__ = replacement", 2),
    ],
)
def test_c1_lazy_getattr_callable_access_holds(tree, tmp_path, case, statement, line):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    assert verify((root, policy)).finite_gate_passed
    add_module(root, "app.tasks.worker", PLANNING_ALIAS + statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", case
    assert result.module_status == "MODULE_IDENTITY_INCONCLUSIVE", case
    assert not result.finite_gate_passed, case
    assert C1_REASON + str(line) in result.reasons, (case, result.reasons)
    assert "WORKER_FACING_LAZY_ACCESS:app.services.planning" in result.reasons
    assert not any(r.startswith("MISSING") for r in result.reasons), result.reasons


@pytest.mark.parametrize(
    "case, statement, line",
    [
        (
            "C1-13-from",
            "from app.services import planning as pl\nfn = pl.__getattr__",
            2,
        ),
        (
            "C1-13-dotted",
            "import app.services.planning\nfn = app.services.planning.__getattr__",
            2,
        ),
        (
            "C1-13-importlib",
            "from importlib import import_module\n"
            "pl = import_module('app.services.planning')\nfn = pl.__getattr__",
            3,
        ),
    ],
)
def test_c1_13_package_alias_callable_extraction_holds(
    tree, tmp_path, case, statement, line
):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", case
    assert not result.finite_gate_passed, case
    assert C1_REASON + str(line) in result.reasons, (case, result.reasons)


@pytest.mark.parametrize(
    "statement, reason",
    [
        ("q = p\nfn = q.__getattr__", "PACKAGE_HANDLE_ESCAPE:app.tasks.worker:2"),
        (
            "fn = (p if flag else other).__getattr__",
            "PACKAGE_HANDLE_ESCAPE:app.tasks.worker:2",
        ),
        (
            "from app.services.planning import __getattr__ as fn",
            "UNRESOLVED_FROM_IMPORT:app.services.planning.__getattr__",
        ),
    ],
)
def test_c1_14_unsupported_alias_flow_remains_hold(tree, tmp_path, statement, reason):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", PLANNING_ALIAS + statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", result.reasons
    assert reason in result.reasons, result.reasons


def test_c1_15_unrelated_object_getattr_does_not_trigger(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(
        root,
        "app.tasks.worker",
        "import app.services.planning\nimport collections\n"
        "from app.services import agents\n"
        "fn = collections.OrderedDict().__getattr__\nother = obj.__getattr__\n"
        "unreviewed = agents.__getattr__\n",
    )
    result = verify((root, policy))
    assert result.finite_gate_passed, result.reasons
    assert not any("LAZY" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "case, statement, child",
    [
        ("C1-16", "import app.services.planning", None),
        (
            "C1-17",
            "from app.services.planning import export",
            "app.services.planning.export",
        ),
        ("C1-18", "", None),
    ],
)
def test_c1_16_18_literal_imports_and_dormant_site_remain_eligible(
    tree, tmp_path, case, statement, child
):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    if statement:
        add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.finite_gate_passed, (case, result.reasons)
    assert result.root_status == "ROOT_VALID"
    assert (
        "DORMANT_UNLOADED_LAZY_SITE:app.services.planning"
        in result.derivation.diagnostics
    )
    if child:
        assert child in result.derivation.required
    assert not any("LAZY" in reason for reason in result.reasons)


def test_c1_19_i12_omission_remains_invalid_with_correction(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    result = verify((root, policy), omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert result.module_status == "MODULE_IDENTITY_INVALID"
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons


def test_c1_20_candidate_policy_substitution_remains_rejected(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    candidate_policy = root / "candidate-policy.json"
    candidate_policy.write_bytes(Path(policy.selected_path).read_bytes())
    with pytest.raises(v.PolicyError, match="CANDIDATE_POLICY_NOT_AUTHORITATIVE"):
        v.load_policy(
            candidate_policy, candidate_root=root, expected_digest=policy.digest
        )


def test_c1_21_candidate_checker_cannot_alter_trusted_derivation(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(
        root,
        "app.services.orchestration.worker_identity",
        "raise RuntimeError('candidate checker executed')\n"
        "LAZY_GETATTR_ALLOWED = True\n",
    )
    add_module(root, "app.tasks.worker", PLANNING_ALIAS + "fn = p.__getattr__\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert C1_REASON + "2" in result.reasons
    assert "app.services.model_adaptation" in result.derivation.required


def test_c1_22_policy_digest_mismatch_remains_rejected(tree):
    root, _ = tree
    with pytest.raises(v.PolicyError, match="POLICY_DIGEST_MISMATCH"):
        v.load_policy(
            FIXTURE, candidate_root=root, expected_digest="sha256:" + "0" * 64
        )


@pytest.mark.parametrize("field", ["source_digest", "site_digest"])
def test_c1_23_source_site_and_map_drift_remain_fail_closed(tree, tmp_path, field):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    body = policy.body
    body["lazy_imports"][0][field] = "sha256:" + "0" * 64
    result = verify((root, selected_policy(tmp_path, root, body)))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert "LAZY_POLICY_IDENTITY_MISMATCH:app.services.planning" in result.reasons
    body = policy.body
    body["lazy_imports"][0]["export_map_digest"] = "sha256:" + "0" * 64
    with pytest.raises(v.PolicyError, match="LAZY_EXPORT_MAP_IDENTITY"):
        selected_policy(tmp_path, root, body)


def test_c1_24_ar6_self_derived_manifest_never_passes_finite_gate(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(
        root,
        "app.tasks.worker",
        "import app.services.planning as p\n"
        "getattr_fn = p.__getattr__\ngetattr_fn('Export')\n",
    )
    derived = v.derive(root, policy)
    result = v.compare(derived, manifest(derived))
    assert derived.root_status == "ROOT_INCONCLUSIVE"
    assert result.module_status == "MODULE_IDENTITY_INCONCLUSIVE"
    assert result.finite_gate_passed is False
    assert C1_REASON + "2" in result.reasons


C2_PREFIX = "import app.services.planning\n"
C2_DYNAMIC = "DYNAMIC_IMPORT_RESULT_ACCESS_UNSUPPORTED:app.tasks.worker:"
C2_REFLECTION = "REFLECTION_UNSUPPORTED:app.tasks.worker:"
IM = "from importlib import import_module\n"
PLAN = "import_module('app.services.planning')"


@pytest.mark.parametrize(
    "case, statement, reason",
    [
        (
            "C2-01",
            "import importlib\nf = importlib.import_module("
            "'app.services.planning').__getattr__",
            C2_DYNAMIC + "3",
        ),
        ("C2-02", IM + PLAN + ".__getattr__('Export')", C2_DYNAMIC + "3"),
        (
            "C2-03",
            "import importlib\nimportlib.import_module("
            "'app.services').planning.__getattr__('Export')",
            C2_DYNAMIC + "3",
        ),
        (
            "C2-04",
            "import importlib\nimportlib.import_module('app.services.planning').Export",
            C2_DYNAMIC + "3",
        ),
        (
            "C2-05",
            "__import__('app.services.planning').services.planning"
            ".__getattr__('Export')",
            C2_DYNAMIC + "2",
        ),
        (
            "C2-06",
            "__import__('app.services.planning').services.planning.Export",
            C2_DYNAMIC + "2",
        ),
        (
            "C2-07",
            "import sys\nf = sys.modules['app.services.planning'].__getattr__",
            C2_REFLECTION + "3",
        ),
        (
            "C2-08",
            "import sys\nsys.modules.get('app.services.planning')"
            ".__getattr__('Export')",
            C2_REFLECTION + "3",
        ),
        (
            "C2-09",
            "import sys\nsys.modules['app.services.planning'].Export",
            C2_REFLECTION + "3",
        ),
        ("C2-10", IM + "getattr(" + PLAN + ", 'Export')", C2_DYNAMIC + "3"),
        ("C2-11", IM + "hasattr(" + PLAN + ", 'Export')", C2_DYNAMIC + "3"),
        ("C2-12", IM + "getattr(" + PLAN + ", '__getattr__')", C2_DYNAMIC + "3"),
        ("C2-13", IM + PLAN + "['Export']", C2_DYNAMIC + "3"),
        (
            "C2-14",
            IM + "import_module('app').services.planning.plan.Export",
            C2_DYNAMIC + "3",
        ),
        ("C2-15", IM + "f = lambda: " + PLAN + ".Export", C2_DYNAMIC + "3"),
        (
            "C2-16",
            IM + "def never():\n    return " + PLAN + ".Export",
            C2_DYNAMIC + "4",
        ),
        ("C2-17", IM + "[" + PLAN + ".Export for _ in ()]", C2_DYNAMIC + "3"),
        ("C2-18", IM + "if flag:\n    " + PLAN + ".Export", C2_DYNAMIC + "4"),
        (
            "C2-19",
            IM + "@" + PLAN + ".__getattr__\ndef decorated():\n    pass",
            C2_DYNAMIC + "3",
        ),
        (
            "C2-20",
            "import sys\nsys.modules['app.services.planning']",
            C2_REFLECTION + "3",
        ),
        (
            "C2-21",
            "import sys\nsys.modules.get('app.services.planning')",
            C2_REFLECTION + "3",
        ),
        ("C2-22", "import sys\ntable = sys.modules", C2_REFLECTION + "3"),
        ("C2-23", "import sys\nget = sys.modules.get", C2_REFLECTION + "3"),
        (
            "C2-24",
            "import sys\ndef helper(name):\n    return sys.modules[name]",
            C2_REFLECTION + "4",
        ),
        ("C2-25", "import sys\nif False:\n    sys.modules", C2_REFLECTION + "4"),
        ("C2-x-from-sys", "from sys import modules", C2_REFLECTION + "2"),
        (
            "C2-x-from-sys-alias",
            "from sys import modules as m\nm['x']",
            C2_REFLECTION + "2",
        ),
        ("C2-x-sys-alias", "import sys as s\ns.modules", C2_REFLECTION + "3"),
        ("C2-x-os-sys", "import os\nos.sys.modules", C2_REFLECTION + "3"),
        ("C2-x-sys-attr", "import os\nhandle = os.sys", C2_REFLECTION + "3"),
        (
            "C2-x-getattr-sys",
            "import sys\ngetattr(sys, 'modules')",
            C2_REFLECTION + "3",
        ),
        (
            "C2-x-getattr-sys-computed",
            "import sys\ngetattr(sys, name)",
            C2_REFLECTION + "3",
        ),
        ("C2-x-getattr-sys-attr", "import os\ngetattr(os, 'sys')", C2_REFLECTION + "3"),
        ("C2-x-sys-store", "import sys\nsys.modules['x'] = None", C2_REFLECTION + "3"),
        ("C2-x-sys-del", "import sys\ndel sys.modules['x']", C2_REFLECTION + "3"),
        ("C2-x-import-sys-call", "__import__('sys').modules", C2_DYNAMIC + "2"),
        (
            "C2-x-external-assign",
            "import importlib\nhandle = importlib.import_module('sys')",
            C2_DYNAMIC + "3",
        ),
        (
            "C2-x-dunder-assign",
            "root_pkg = __import__('app.services.planning')\n"
            "root_pkg.services.planning.Export",
            C2_DYNAMIC + "2",
        ),
        (
            "C2-x-relative-assign",
            IM + "pl = import_module('.planning', 'app.services')\npl.Export",
            C2_DYNAMIC + "3",
        ),
        ("C2-x-helper", IM + "helper(" + PLAN + ")", C2_DYNAMIC + "3"),
        ("C2-x-return", IM + "def leak():\n    return " + PLAN, C2_DYNAMIC + "4"),
        ("C2-x-container", IM + "box = [" + PLAN + "]", C2_DYNAMIC + "3"),
        ("C2-x-tuple-assign", IM + "a, b = " + PLAN + ", 1", C2_DYNAMIC + "3"),
    ],
)
def test_c2_computed_module_handle_holds(tree, tmp_path, case, statement, reason):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.services.planning.plan", "Export = 1\n")
    assert verify((root, policy)).finite_gate_passed
    add_module(root, "app.tasks.worker", C2_PREFIX + statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", case
    assert result.module_status == "MODULE_IDENTITY_INCONCLUSIVE", case
    assert not result.finite_gate_passed, case
    assert reason in result.reasons, (case, result.reasons)
    assert not any(r.startswith("MISSING") for r in result.reasons), result.reasons


@pytest.mark.parametrize(
    "case, statement",
    [
        ("C2-26", "import sys\nversion = sys.version_info\nsys.path"),
        ("C2-27", IM + PLAN),
        (
            "C2-27-child",
            "import importlib\nimportlib.import_module('app.services.planning.export')",
        ),
        ("C2-27-dunder", "__import__('app.services.planning')"),
        ("C2-27-relative", IM + "import_module('.planning', 'app.services')"),
        ("C2-28", IM + "pl = " + PLAN),
        (
            "C2-28-child",
            IM + "pc = import_module('app.services.planning.export')\npc.Export",
        ),
    ],
)
def test_c2_supported_imports_remain_eligible(tree, tmp_path, case, statement):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", C2_PREFIX + statement + "\n")
    result = verify((root, policy))
    assert result.finite_gate_passed, (case, result.reasons)
    assert result.root_status == "ROOT_VALID"
    assert (
        "DORMANT_UNLOADED_LAZY_SITE:app.services.planning"
        in result.derivation.diagnostics
    )


@pytest.mark.parametrize(
    "case, statement, reason",
    [
        (
            "C2-29",
            "import app.services.planning as p\np.Export",
            "WORKER_FACING_LAZY_ACCESS:app.services.planning",
        ),
        (
            "C2-29-c1",
            "import app.services.planning as p\nfn = p.__getattr__",
            "LAZY_GETATTR_CALLABLE_ACCESS:app.tasks.worker:2",
        ),
        (
            "C2-31",
            "import app.services.planning as p\n"
            "getattr_fn = p.__getattr__\ngetattr_fn('Export')",
            "LAZY_GETATTR_CALLABLE_ACCESS:app.tasks.worker:2",
        ),
        (
            "C2-37",
            "import importlib.util\nimportlib.util.spec_from_file_location('x', 'y')",
            "UNKNOWN_LOADER:app.tasks.worker:2",
        ),
        (
            "C2-38",
            "__import__('app.services.planning', fromlist=[name])",
            "UNSUPPORTED_IMPORT_ARGUMENTS:app.tasks.worker",
        ),
        (
            "C2-38-computed",
            "import importlib\nimportlib.import_module(name)",
            "UNRESOLVED_DYNAMIC_IMPORT:app.tasks.worker:2",
        ),
        (
            "C2-39",
            "import sys\nsys.meta_path.append(hook)",
            "IMPORT_PATH_MUTATION:app.tasks.worker",
        ),
        (
            "C2-39-path-hooks",
            "import sys\nsys.path_hooks.insert(0, hook)",
            "IMPORT_PATH_MUTATION:app.tasks.worker",
        ),
    ],
)
def test_c2_existing_holds_preserved(tree, tmp_path, case, statement, reason):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE", (case, result.reasons)
    assert not result.finite_gate_passed
    assert reason in result.reasons, (case, result.reasons)


def test_c2_30_exact_dormant_site_remains_eligible(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    result = verify((root, policy))
    assert result.finite_gate_passed, result.reasons
    assert [site["module"] for site in result.derivation.dormant_lazy_sites] == [
        "app.services.planning"
    ]


def test_c2_32_i12_omission_remains_invalid(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", "import sys\nsys.modules\n")
    result = verify((root, policy), omit="app.services.model_adaptation")
    assert result.root_status == "ROOT_INVALID"
    assert "MISSING_REQUIRED_IDENTITY:app.services.model_adaptation" in result.reasons


def test_c2_33_candidate_policy_substitution_rejected(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    candidate_policy = root / "app" / "candidate-policy.json"
    candidate_policy.write_bytes(Path(policy.selected_path).read_bytes())
    with pytest.raises(v.PolicyError, match="CANDIDATE_POLICY_NOT_AUTHORITATIVE"):
        v.load_policy(
            candidate_policy, candidate_root=root, expected_digest=policy.digest
        )


def test_c2_34_candidate_checker_cannot_alter_derivation(tree, tmp_path):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(
        root,
        "app.services.orchestration.worker_identity",
        "raise RuntimeError('candidate checker executed')\nSYS_MODULES_OK = True\n",
    )
    add_module(root, "app.tasks.worker", C2_PREFIX + "import sys\nsys.modules\n")
    result = verify((root, policy))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert C2_REFLECTION + "3" in result.reasons
    assert "app.services.model_adaptation" in result.derivation.required


def test_c2_35_policy_digest_mismatch_rejected(tree):
    root, _ = tree
    with pytest.raises(v.PolicyError, match="POLICY_DIGEST_MISMATCH"):
        v.load_policy(
            FIXTURE, candidate_root=root, expected_digest="sha256:" + "2" * 64
        )


@pytest.mark.parametrize("field", ["source_digest", "site_digest"])
def test_c2_36_identity_drift_rejected(tree, tmp_path, field):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    body = policy.body
    body["lazy_imports"][0][field] = "sha256:" + "3" * 64
    result = verify((root, selected_policy(tmp_path, root, body)))
    assert "LAZY_POLICY_IDENTITY_MISMATCH:app.services.planning" in result.reasons
    body = policy.body
    body["lazy_imports"][0]["export_map_digest"] = "sha256:" + "3" * 64
    with pytest.raises(v.PolicyError, match="LAZY_EXPORT_MAP_IDENTITY"):
        selected_policy(tmp_path, root, body)


@pytest.mark.parametrize(
    "statement",
    [
        "import importlib\nf = importlib.import_module('app.services.planning')"
        ".__getattr__\nf('Export')",
        "import sys\nsys.modules['app.services.planning'].Export",
    ],
)
def test_c2_40_self_derived_manifest_never_passes_gate(tree, tmp_path, statement):
    root, policy, _ = reviewed_planning_tree(tree, tmp_path)
    add_module(root, "app.tasks.worker", C2_PREFIX + statement + "\n")
    derived = v.derive(root, policy)
    result = v.compare(derived, manifest(derived))
    assert derived.root_status == "ROOT_INCONCLUSIVE"
    assert result.module_status == "MODULE_IDENTITY_INCONCLUSIVE"
    assert result.finite_gate_passed is False


PLANNING_INIT = REPO / "app/services/planning/__init__.py"
WORKER = REPO / "app/tasks/worker.py"
OWNERSHIP = REPO / "app/services/execution/execution_task_runtime_ownership_service.py"


def top_level_names(path):
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def test_sc2_c1_planning_initializer_is_docstring_only():
    module = ast.parse(PLANNING_INIT.read_text())
    assert len(module.body) == 1
    assert isinstance(module.body[0], ast.Expr)
    assert isinstance(module.body[0].value, ast.Constant)
    assert isinstance(module.body[0].value.value, str)
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign))
        for node in ast.walk(module)
    )
    assert not {"__getattr__", "_EXPORTS", "__all__"} & top_level_names(PLANNING_INIT)


def test_sc2_c1_concrete_planning_submodule_imports_resolve():
    checked = 0
    for path in (REPO / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and (node.module or "").startswith("app.services.planning")
            ):
                continue
            base = REPO / node.module.replace(".", "/")
            module_path = (
                base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
            )
            assert module_path.is_file(), (path, node.lineno)
            defined = top_level_names(module_path)
            for alias in node.names:
                assert alias.name != "*", (path, node.lineno)
                assert (
                    alias.name in defined
                    or (base / (alias.name + ".py")).is_file()
                    or (base / alias.name / "__init__.py").is_file()
                ), (path, node.lineno, alias.name)
                checked += 1
    assert checked


def receive_dispatch_function():
    return next(
        node
        for node in ast.parse(WORKER.read_text()).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "receive_execution_task_dispatch"
    )


def test_sc2_c1_w1_ownership_import_is_function_local_and_static():
    function = receive_dispatch_function()
    imports = [
        node for node in function.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert len(imports) == 1
    assert imports[0].module == "app.services.execution"
    assert imports[0].level == 0
    assert [(a.name, a.asname) for a in imports[0].names] == [
        ("execution_task_runtime_ownership_service", "ownership_module")
    ]
    assert not any(
        isinstance(node, ast.Name)
        and node.id == "import_module"
        or isinstance(node, ast.Attribute)
        and node.attr == "import_module"
        for node in ast.walk(ast.parse(WORKER.read_text()))
    )
    module_imports = [
        alias.name if isinstance(node, ast.Import) else f"{node.module}.{alias.name}"
        for node in ast.parse(WORKER.read_text()).body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert not any(
        "execution_task_runtime_ownership_service" in name for name in module_imports
    )


def test_sc2_c1_w1_binds_existing_service_and_error():
    bindings = {
        node.targets[0].id: node.value
        for node in receive_dispatch_function().body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    service = bindings["ownership_service"]
    assert isinstance(service, ast.Call) and service.func.id == "getattr"
    assert service.args[0].id == "ownership_module"
    name = eval(compile(ast.Expression(service.args[1]), "<w1>", "eval"), {})
    assert name == "ExecutionTaskRuntimeOwnershipService"
    error = bindings["ownership_error"]
    assert isinstance(error, ast.Attribute) and error.value.id == "ownership_module"
    assert error.attr == "ExecutionRuntimeOwnershipError"
    assert {name, error.attr} <= top_level_names(OWNERSHIP)


def test_sc2_c1_w1_keeps_phase29c_substring_guard_inapplicable():
    # test_phase29c1 scans files containing this substring for status writes.
    assert "ExecutionTask" not in WORKER.read_text()


def test_sc2_c1_w1_shape_is_a_normal_verifier_edge(tree):
    root, _ = tree
    add_module(
        root,
        "app.tasks.worker",
        "def receive():\n"
        "    from app.services.execution import (\n"
        "        execution_task_runtime_ownership_service as ownership_module,\n"
        "    )\n"
        "    service = getattr(ownership_module, 'Execution' + 'TaskX')\n"
        "    return service, ownership_module.ExecutionRuntimeOwnershipError\n",
    )
    result = verify(tree)
    assert result.finite_gate_passed, result.reasons
    assert "app.services.execution.execution_task_runtime_ownership_service" in {
        edge.target
        for edge in result.derivation.edges
        if edge.source == "app.tasks.worker"
    }
