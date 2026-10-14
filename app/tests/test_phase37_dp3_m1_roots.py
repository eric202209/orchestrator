"""Disposable AST-only fixtures; run with scripts/verification/run_root_tests.py.

No app imports, candidate execution, application conftest or production state.
"""

from __future__ import annotations

import ast
import copy
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
    assert any("LAZY_REVIEW_REQUIRED" in r for r in result.reasons)


def test_exact_reviewed_lazy_exception_and_new_worker_access(tree, tmp_path):
    root, policy = tree
    source = "_EXPORTS = {'Export': ('app.services.planning.leaf', 'Export')}\ndef __getattr__(name):\n    module_name, attr_name = _EXPORTS[name]\n    from importlib import import_module\n    return getattr(import_module(module_name), attr_name)\n"
    path = add_module(root, "app.services.planning", source, package=True)
    add_module(root, "app.tasks.worker", "import app.services.planning\n")
    function = ast.parse(source).body[1]
    body = policy.body
    body["lazy_imports"] = [
        {
            "module": "app.services.planning",
            "source_digest": v.digest(path.read_bytes()),
            "site_digest": v.digest(
                ast.dump(function, include_attributes=True).encode()
            ),
            "review_record_id": "synthetic-independent-review-only",
            "export_names": ["Export"],
            "mechanism": "getattr-export-map",
        }
    ]
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
    first = v.derive(root, policy)
    assert first == v.derive(root, policy)
    assert first.root_status == "ROOT_VALID"
    assert not any(name == "app" or name.startswith("app.") for name in sys.modules)


@pytest.mark.parametrize(
    "statement",
    [
        "import app.services.planning as p\np.Export",
        "import app.services.planning as p\nconsumer(p)",
        "from importlib import import_module\np = import_module('app.services.planning')",
    ],
)
def test_reviewed_lazy_alias_access_holds(tree, tmp_path, statement):
    root, policy = tree
    source = "_EXPORTS = {'Export': ('app.services.planning.leaf', 'Export')}\ndef __getattr__(name):\n    module_name, attr_name = _EXPORTS[name]\n    from importlib import import_module\n    return getattr(import_module(module_name), attr_name)\n"
    path = add_module(root, "app.services.planning", source, package=True)
    function = ast.parse(source).body[1]
    body = policy.body
    body["lazy_imports"] = [
        {
            "module": "app.services.planning",
            "source_digest": v.digest(path.read_bytes()),
            "site_digest": v.digest(
                ast.dump(function, include_attributes=True).encode()
            ),
            "review_record_id": "synthetic-only",
            "export_names": ["Export"],
            "mechanism": "getattr-export-map",
        }
    ]
    selected = selected_policy(tmp_path, root, body)
    add_module(root, "app.tasks.worker", statement + "\n")
    result = verify((root, selected))
    assert result.root_status == "ROOT_INCONCLUSIVE"
    assert "WORKER_FACING_LAZY_ACCESS:app.services.planning" in result.reasons


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
    body["lazy_imports"] = [
        {
            "module": "app.services.planning",
            "source_digest": v.digest(path.read_bytes()),
            "site_digest": v.digest(
                ast.dump(ast.parse(source).body[0], include_attributes=True).encode()
            ),
            "review_record_id": "synthetic-only",
            "export_names": ["Export"],
            "mechanism": "getattr-export-map",
        }
    ]
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
