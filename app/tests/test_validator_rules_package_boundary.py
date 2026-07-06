"""Phase 20J: validator rules package boundary documentation.

Prework for the validator rule split (docs/roadmap/refactoring-phases.md).
`app/services/orchestration/validation/rules/` is an additive skeleton as
of Phase 20J: no rule implementation has moved there yet. This test
documents (and enforces) the intended ownership split so a later phase
cannot drift from it silently:

- `core_*` modules will own `core_invariant` rules (structural checks that
  hold regardless of workload).
- `contract_*` modules will own `workload_contract` rules (reusable,
  workload-family-scoped checks, per the taxonomy in
  `app/services/orchestration/rule_registry.py`).
- `rule_registry.py` remains a documentation/contract lookup table, not a
  runtime dependency: nothing in the orchestration runtime imports from it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PACKAGE_DIR = (
    REPO_ROOT / "app" / "services" / "orchestration" / "validation" / "rules"
)

CORE_MODULES = {"core_schema", "core_paths", "core_file_ops"}
CONTRACT_MODULES = {
    "contract_placeholders",
    "contract_python",
    "contract_frontend",
    "contract_commands",
    "contract_verification",
}


def test_rules_package_exists_with_expected_modules():
    assert RULES_PACKAGE_DIR.is_dir(), (
        f"{RULES_PACKAGE_DIR} is missing. Phase 20J creates this package "
        "as an additive skeleton before any rule implementation moves."
    )
    assert (RULES_PACKAGE_DIR / "__init__.py").is_file()

    present_modules = {
        path.stem for path in RULES_PACKAGE_DIR.glob("*.py") if path.stem != "__init__"
    }
    expected_modules = CORE_MODULES | CONTRACT_MODULES
    assert present_modules == expected_modules, (
        "Unexpected rules-package module set. "
        f"Missing: {expected_modules - present_modules}; "
        f"Unexpected: {present_modules - expected_modules}"
    )


def test_core_modules_are_named_for_core_invariant_ownership():
    for name in CORE_MODULES:
        assert name.startswith("core_"), (
            f"{name} is expected to own core_invariant rules per the "
            "app/services/orchestration/rule_registry.py taxonomy and must "
            "use the core_ prefix."
        )


def test_contract_modules_are_named_for_workload_contract_ownership():
    for name in CONTRACT_MODULES:
        assert name.startswith("contract_"), (
            f"{name} is expected to own workload_contract rules per the "
            "app/services/orchestration/rule_registry.py taxonomy and must "
            "use the contract_ prefix."
        )


def test_core_modules_now_own_core_invariant_rule_logic():
    """Phase 20M moved core-invariant rule helpers into `core_*` modules."""
    for name in CORE_MODULES:
        path = RULES_PACKAGE_DIR / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defs = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert defs, (
            f"{path.name} is expected to contain core_invariant rule "
            "implementation after Phase 20M."
        )


def _orchestration_runtime_source_files():
    orchestration_dir = REPO_ROOT / "app" / "services" / "orchestration"
    for path in orchestration_dir.rglob("*.py"):
        if path.name == "rule_registry.py":
            continue
        if "validation/rules" in str(path.relative_to(REPO_ROOT)).replace("\\", "/"):
            continue
        yield path


def test_rule_registry_is_not_imported_by_orchestration_runtime():
    """rule_registry.py is a documentation/contract lookup table only.

    It must never become a runtime dependency of the orchestration
    services (planner, validator, execution, recovery) — only tests are
    allowed to import it to cross-check ownership.
    """
    offenders = []
    for path in _orchestration_runtime_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith("rule_registry"):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("rule_registry"):
                        offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "rule_registry.py must remain a documentation/contract lookup "
        f"table, not a runtime dependency. Importing files: {offenders}"
    )
