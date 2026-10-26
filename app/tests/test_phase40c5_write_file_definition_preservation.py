"""PHASE40C5 whole-file write preservation regressions.

Workflows A (Task 19) and B (Task 20) accepted Plans whose ``write_file``
content for an existing flat-layout module kept only one function, dropping
imports and helpers that the new content still referenced. The candidates
then failed collection with ``NameError``. These tests replay those exact
operations against the original seed sources.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.orchestration.validation.rules.contract_python import (
    _plan_python_source_syntax_issues,
    _plan_write_file_drops_referenced_python_definitions,
)
from app.services.orchestration.validation.validator import ValidatorService

CHECKOUT_SEED = '''"""Checkout totals."""

from order_service.models import Order
from order_service.pricing import discount_for, tax_for


def checkout_total(order: Order, tax_rate_bps: int) -> int:
    """Return the amount to charge in cents: discounted subtotal plus tax."""
    discounted = discount_for(order.subtotal_cents, order.coupon_code)
    return discounted + tax_for(discounted, tax_rate_bps)
'''

# Exact Task 19 / Execution 19 accepted write_file content.
CHECKOUT_A2_CONTENT = (
    "def checkout_total(order: Order, tax_rate_bps: int) -> int:\n"
    '    """Return the amount to charge in cents: discounted subtotal plus tax."""\n'
    "    discounted = order.subtotal_cents - discount_for(order.subtotal_cents, "
    "order.coupon_code)\n"
    "    return discounted + tax_for(discounted, tax_rate_bps)"
)

LOADER_SEED = '''"""Minimal typed key=value configuration loader."""

from typing import Dict, Mapping


class ConfigError(ValueError):
    """Raised for malformed configuration or values of the wrong type."""


def parse_config(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _coerce(key: str, raw: str, kind: type):
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{key}: invalid integer {raw!r}") from None
    return raw


def load_config(text: str, schema: Mapping[str, type]) -> dict:
    raw = parse_config(text)
    result = {}
    for key, kind in schema.items():
        if key not in raw:
            raise ConfigError(f"{key}: missing")
        result[key] = _coerce(key, raw[key], kind)
    return result
'''

# Task 20 / Execution 20 accepted write_file content (function only).
LOADER_B1_CONTENT = (
    'def load_config(text: str, schema: Mapping[str, type], env=None, prefix="APP_")'
    " -> dict:\n"
    "    raw = parse_config(text)\n"
    "    result = {}\n"
    "    for key, kind in schema.items():\n"
    "        if key not in raw:\n"
    '            raise ConfigError(f"{key}: missing")\n'
    "        raw_value = raw[key]\n"
    "        if env is not None and prefix + key.upper() in env:\n"
    "            raw_value = env[prefix + key.upper()]\n"
    "        result[key] = _coerce(key, raw_value, kind)\n"
    "    return result"
)


def _seed_orders(root: Path) -> None:
    package = root / "order_service"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "checkout.py").write_text(CHECKOUT_SEED, encoding="utf-8")
    (package / "models.py").write_text("class Order:\n    pass\n", encoding="utf-8")
    (package / "pricing.py").write_text(
        "def discount_for(a, b):\n    return 0\n\n\ndef tax_for(a, b):\n    return 0\n",
        encoding="utf-8",
    )


def _seed_config(root: Path) -> None:
    package = root / "configlib"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from configlib.loader import ConfigError, load_config, parse_config\n",
        encoding="utf-8",
    )
    (package / "loader.py").write_text(LOADER_SEED, encoding="utf-8")


def _plan(*ops: dict) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Edit the module",
            "commands": [],
            "verification": "python -m pytest -q tests",
            "rollback": None,
            "expected_files": [op["path"] for op in ops],
            "ops": list(ops),
        }
    ]


def _write(path: str, content: str) -> dict:
    return {"op": "write_file", "path": path, "content": content}


def _validate(plan: list[dict], root: Path, prompt: str):
    result = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=prompt,
        execution_profile="full_lifecycle",
        project_dir=root,
        title=prompt,
        description=prompt,
    )
    return getattr(result, "verdict", result)


def test_workflow_a_fragment_dropping_imports_is_flagged(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(_write("order_service/checkout.py", CHECKOUT_A2_CONTENT))

    findings = _plan_write_file_drops_referenced_python_definitions(plan, tmp_path)

    assert findings == [
        {
            "path": "order_service/checkout.py",
            "step_number": 1,
            "dropped_referenced_names": ["Order", "discount_for", "tax_for"],
        }
    ]


def test_workflow_b_fragment_dropping_helpers_is_flagged(tmp_path):
    _seed_config(tmp_path)
    plan = _plan(_write("configlib/loader.py", LOADER_B1_CONTENT))

    findings = _plan_write_file_drops_referenced_python_definitions(plan, tmp_path)

    assert [finding["dropped_referenced_names"] for finding in findings] == [
        ["ConfigError", "Mapping", "_coerce", "parse_config"]
    ]


def test_validate_plan_requires_repair_for_dropped_definitions(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(_write("order_service/checkout.py", CHECKOUT_A2_CONTENT))

    verdict = _validate(
        plan, tmp_path, "Fix checkout_total in order_service/checkout.py"
    )

    assert verdict.status == "repair_required"
    reason = next(
        reason
        for reason in verdict.reasons
        if "python_write_file_drops_referenced_definitions" in reason
    )
    assert "order_service/checkout.py: Order, discount_for, tax_for" in reason
    assert "replace_in_file" in reason
    assert verdict.details["python_write_file_drops_referenced_definitions"][0][
        "dropped_referenced_names"
    ] == ["Order", "discount_for", "tax_for"]


def test_complete_whole_file_rewrite_is_not_flagged(tmp_path):
    _seed_orders(tmp_path)
    corrected = CHECKOUT_SEED.replace(
        "discounted = discount_for(",
        "discounted = order.subtotal_cents - discount_for(",
    )
    plan = _plan(_write("order_service/checkout.py", corrected))

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []


def test_rewrite_that_removes_unused_definitions_is_not_flagged(tmp_path):
    _seed_config(tmp_path)
    rewritten = (
        "def load_config(text, schema, env=None, prefix='APP_'):\n    return {}\n"
    )
    plan = _plan(_write("configlib/loader.py", rewritten))

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []


def test_localized_replace_in_file_is_not_flagged(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(
        {
            "op": "replace_in_file",
            "path": "order_service/checkout.py",
            "old": "discounted = discount_for(",
            "new": "discounted = order.subtotal_cents - discount_for(",
        }
    )

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []
    verdict = _validate(
        plan, tmp_path, "Fix checkout_total in order_service/checkout.py"
    )
    assert not verdict.details.get("python_write_file_drops_referenced_definitions")


def test_new_module_creation_is_not_flagged(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(_write("order_service/refunds.py", CHECKOUT_A2_CONTENT))

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []


def test_locally_rebound_and_builtin_names_are_not_flagged(tmp_path):
    _seed_orders(tmp_path)
    content = (
        "def checkout_total(order, tax_rate_bps):\n"
        "    from order_service.pricing import discount_for, tax_for\n"
        "    discounted = order.subtotal_cents - discount_for(order.subtotal_cents, None)\n"
        "    return int(discounted + tax_for(discounted, tax_rate_bps))\n"
    )
    plan = _plan(_write("order_service/checkout.py", content))

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []


def test_star_import_and_postponed_annotations_are_not_flagged(tmp_path):
    _seed_orders(tmp_path)
    star = "from order_service.pricing import *\n\n\ndef total(order: Order):\n    return tax_for(1, 2)\n"
    postponed = (
        "from __future__ import annotations\n\n"
        "from order_service.pricing import discount_for, tax_for\n\n\n"
        "def checkout_total(order: Order, tax_rate_bps: int) -> int:\n"
        "    return tax_for(discount_for(1, None), tax_rate_bps)\n"
    )

    for content in (star, postponed):
        plan = _plan(_write("order_service/checkout.py", content))
        assert (
            _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []
        )


def test_invalid_syntax_is_left_to_the_syntax_gate(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(_write("order_service/checkout.py", "def checkout_total(:\n"))

    assert _plan_write_file_drops_referenced_python_definitions(plan, tmp_path) == []
    assert _plan_python_source_syntax_issues(plan, tmp_path)[0]["path"] == (
        "order_service/checkout.py"
    )


def test_second_write_is_compared_with_the_simulated_first_write(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(
        _write("order_service/checkout.py", "def helper():\n    return 1\n"),
        _write("order_service/checkout.py", "def total():\n    return helper()\n"),
    )

    findings = _plan_write_file_drops_referenced_python_definitions(plan, tmp_path)

    assert [finding["dropped_referenced_names"] for finding in findings] == [["helper"]]


def test_stale_replace_in_file_still_rejected_by_existing_gate(tmp_path):
    _seed_orders(tmp_path)
    plan = _plan(
        {
            "op": "replace_in_file",
            "path": "order_service/checkout.py",
            "old": "text that is not in the file",
            "new": "anything",
        }
    )

    verdict = _validate(
        plan, tmp_path, "Fix checkout_total in order_service/checkout.py"
    )

    assert verdict.status == "repair_required"
    assert any(reason.startswith("stale_replace:") for reason in verdict.reasons)
    assert not verdict.details.get("python_write_file_drops_referenced_definitions")
