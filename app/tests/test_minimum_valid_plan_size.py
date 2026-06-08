"""
Characterization: minimum valid plan size per fixture.

Measures the smallest validator-accepted plan for:
  - tiny_money_source_rewrite
  - stale_replace_repair
  - Garden Story Task 1 (static-site create)

Reports chars, estimated tokens, and per-field breakdown.

Investigation only — no schema, validator, or prompt changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.services.orchestration.validation.validator import ValidatorService

# ---------------------------------------------------------------------------
# Token estimation (no tiktoken in this env)
# GPT-family JSON tokenizes at ~3.2 chars/token for dense JSON.
# Qwen3 uses a similar BPE vocabulary. Conservative estimate used.
# ---------------------------------------------------------------------------
_CHARS_PER_TOKEN = 3.2


def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


def _json_compact(plan: Any) -> str:
    return json.dumps(plan, separators=(",", ":"))


def _json_pretty(plan: Any) -> str:
    return json.dumps(plan, indent=2)


# ---------------------------------------------------------------------------
# Field-level measurement helpers
# ---------------------------------------------------------------------------


def _field_chars(step: Dict[str, Any], field: str) -> int:
    """Chars consumed by serializing one field's value (not the key)."""
    value = step.get(field)
    return len(json.dumps(value, separators=(",", ":")))


def _key_overhead(key: str) -> int:
    """JSON key overhead: '"key":'"""
    return len(f'"{key}":')


_ALL_FIELDS = [
    "step_number",
    "description",
    "commands",
    "verification",
    "rollback",
    "expected_files",
    "ops",
]


def measure_plan(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return char/token breakdown for a plan."""
    compact = _json_compact(plan)
    pretty = _json_pretty(plan)

    total_chars = len(compact)
    total_tokens = _estimate_tokens(compact)

    per_field_chars: Dict[str, int] = {}
    per_field_tokens: Dict[str, int] = {}
    for field in _ALL_FIELDS:
        chars = sum(_field_chars(step, field) for step in plan if field in step)
        # add key overhead per step
        key_cost = sum(_key_overhead(field) for step in plan if field in step)
        total_field_chars = chars + key_cost
        per_field_chars[field] = total_field_chars
        per_field_tokens[field] = _estimate_tokens(
            json.dumps(
                {field: step.get(field) for step in plan if field in step},
                separators=(",", ":"),
            )
        )

    # JSON structural overhead: brackets, braces, commas between steps
    # Approximate: total - sum(field values)
    value_chars = sum(
        sum(_field_chars(step, f) + _key_overhead(f) for f in step.keys())
        for step in plan
    )
    structural_chars = total_chars - value_chars

    return {
        "step_count": len(plan),
        "compact_chars": total_chars,
        "pretty_chars": len(pretty),
        "estimated_tokens": total_tokens,
        "per_field_chars": per_field_chars,
        "per_field_tokens": per_field_tokens,
        "structural_overhead_chars": max(0, structural_chars),
        "structural_overhead_tokens": _estimate_tokens("[]{}," * len(plan) * 3),
    }


def validate_and_measure(
    plan: List[Dict[str, Any]],
    *,
    task_prompt: str,
    execution_profile: str,
    project_dir: Optional[Path] = None,
    label: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Run ValidatorService and measure plan size. Returns (status, measurements)."""
    schema_result = ValidatorService.validate_plan_schema(plan)
    if not schema_result["valid"]:
        return "schema_invalid", {
            "schema_errors": schema_result["errors"],
            **measure_plan(plan),
        }

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=_json_compact(plan),
        task_prompt=task_prompt,
        execution_profile=execution_profile,
        project_dir=project_dir,
        title=label,
    )
    status = getattr(outcome, "status", str(outcome))
    if hasattr(outcome, "verdict"):
        status = str(outcome.verdict)

    # normalise status string
    from app.services.orchestration.types import (
        PlanAccepted,
        PlanRejected,
        PlanRepairRequired,
    )

    if isinstance(outcome, PlanAccepted):
        status = "accepted"
    elif isinstance(outcome, PlanRejected):
        status = "rejected"
    elif isinstance(outcome, PlanRepairRequired):
        status = "repair_required"

    return status, measure_plan(plan)


# ---------------------------------------------------------------------------
# Fixture: tiny_money_source_rewrite
# One-file Python source rewrite, existing tests, no new files.
# ---------------------------------------------------------------------------

TINY_MONEY_PATH = "src/tiny_money/money.py"
TINY_MONEY_VERIFY = "python3 -m pytest -q"
TINY_MONEY_CONTENT = '''\
"""Money formatting helpers for the tiny money fixture."""


def format_cents(cents: int) -> str:
    """Render integer cents as a dollar amount with two decimal places."""
    sign = "-" if cents < 0 else ""
    abs_cents = abs(cents)
    dollars = abs_cents // 100
    remainder = abs_cents % 100
    return f"{sign}${dollars}.{remainder:02d}"
'''
TINY_MONEY_TASK = (
    "Fix the existing money formatter in src/tiny_money/money.py so the "
    "existing tests pass. Edit only that source file. Do not create new files. "
    "Do not edit tests. Verify with python3 -m pytest -q."
)


def _tiny_money_1step() -> List[Dict[str, Any]]:
    """Minimum: single step with write_file op + verification command."""
    return [
        {
            "step_number": 1,
            "description": "Rewrite format_cents to render integer cents as dollar amounts",
            "commands": [TINY_MONEY_VERIFY],
            "verification": TINY_MONEY_VERIFY,
            "rollback": f"git checkout -- {TINY_MONEY_PATH}",
            "expected_files": [TINY_MONEY_PATH],
            "ops": [
                {
                    "op": "write_file",
                    "path": TINY_MONEY_PATH,
                    "content": TINY_MONEY_CONTENT,
                }
            ],
        }
    ]


def _tiny_money_2step() -> List[Dict[str, Any]]:
    """Canonical: write step + separate verify step."""
    return [
        {
            "step_number": 1,
            "description": "Rewrite format_cents to render integer cents as dollar amounts",
            "commands": [],
            "verification": TINY_MONEY_VERIFY,
            "rollback": f"git checkout -- {TINY_MONEY_PATH}",
            "expected_files": [TINY_MONEY_PATH],
            "ops": [
                {
                    "op": "write_file",
                    "path": TINY_MONEY_PATH,
                    "content": TINY_MONEY_CONTENT,
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Verify tests pass",
            "commands": [TINY_MONEY_VERIFY],
            "verification": TINY_MONEY_VERIFY,
            "rollback": None,
            "expected_files": [],
        },
    ]


# ---------------------------------------------------------------------------
# Fixture: stale_replace_repair
# Replace function body via replace_in_file or full write_file.
# ---------------------------------------------------------------------------

STALE_REPLACE_PATH = "src/stale_replace/summary.py"
STALE_REPLACE_VERIFY = "python3 -m pytest -q"
STALE_REPLACE_OLD = (
    "    counts = Counter(item.strip().lower() for item in items if item.strip())\n"
    '    lines = [f"item={name}; quantity={count}" for name, count in counts.items()]\n'
    '    return "\\n".join(lines)'
)
STALE_REPLACE_NEW = (
    "    counts = Counter(item.strip().lower() for item in items if item.strip())\n"
    '    lines = [f"{name}: {count}" for name, count in sorted(counts.items())]\n'
    '    return "\\n".join(lines)'
)
STALE_REPLACE_FULL_CONTENT = """\
from __future__ import annotations

from collections import Counter


def render_inventory(items: list[str]) -> str:
    counts = Counter(item.strip().lower() for item in items if item.strip())
    lines = [f"{name}: {count}" for name, count in sorted(counts.items())]
    return "\\n".join(lines)
"""
STALE_REPLACE_TASK = (
    "Fix the failing inventory summary tests without weakening tests. "
    "Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."
)


def _stale_replace_write_1step() -> List[Dict[str, Any]]:
    """Minimum: write_file replacing full file content."""
    return [
        {
            "step_number": 1,
            "description": "Rewrite render_inventory to sort items and use name: count format",
            "commands": [STALE_REPLACE_VERIFY],
            "verification": STALE_REPLACE_VERIFY,
            "rollback": f"git checkout -- {STALE_REPLACE_PATH}",
            "expected_files": [STALE_REPLACE_PATH],
            "ops": [
                {
                    "op": "write_file",
                    "path": STALE_REPLACE_PATH,
                    "content": STALE_REPLACE_FULL_CONTENT,
                }
            ],
        }
    ]


def _stale_replace_repair_1step(
    project_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Minimum: replace_in_file for surgical fix (requires existing file)."""
    return [
        {
            "step_number": 1,
            "description": "Fix render_inventory to sort items and use name: count format",
            "commands": [STALE_REPLACE_VERIFY],
            "verification": STALE_REPLACE_VERIFY,
            "rollback": f"git checkout -- {STALE_REPLACE_PATH}",
            "expected_files": [STALE_REPLACE_PATH],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": STALE_REPLACE_PATH,
                    "old": STALE_REPLACE_OLD,
                    "new": STALE_REPLACE_NEW,
                }
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Fixture: Garden Story Task 1 (static-site create)
# Create index.html, css/style.css, images/flower-bg.svg from scratch.
# ---------------------------------------------------------------------------

GARDEN_TASK = (
    "Create a static flower landing page for a Garden Story microsite. "
    "Files required: index.html, css/style.css, images/flower-bg.svg. "
    "Use relative paths. No nested project folders. "
    "Verify files exist after creation."
)
GARDEN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Garden Story</title>
  <link rel="stylesheet" href="css/style.css">
</head>
<body>
  <main>
    <img src="images/flower-bg.svg" alt="Flower background">
    <h1>Garden Story</h1>
  </main>
</body>
</html>
"""
GARDEN_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: sans-serif; background: #f5f0e8; }
main { max-width: 800px; margin: 2rem auto; text-align: center; }
h1 { color: #4a7c59; margin-top: 1rem; }
img { max-width: 100%; height: auto; }
"""
GARDEN_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <circle cx="100" cy="100" r="40" fill="#ffcc44"/>
  <ellipse cx="100" cy="50" rx="20" ry="35" fill="#ff6688"/>
  <ellipse cx="100" cy="150" rx="20" ry="35" fill="#ff6688"/>
  <ellipse cx="50" cy="100" rx="35" ry="20" fill="#ff6688"/>
  <ellipse cx="150" cy="100" rx="35" ry="20" fill="#ff6688"/>
</svg>
"""
GARDEN_VERIFY = (
    'node -e "'
    "require('fs').existsSync('index.html') && "
    "require('fs').existsSync('css/style.css') && "
    "require('fs').existsSync('images/flower-bg.svg') || process.exit(1)"
    '"'
)


def _garden_1step() -> List[Dict[str, Any]]:
    """Minimum 1-step: mkdir + all three write_file ops, inline verify."""
    return [
        {
            "step_number": 1,
            "description": "Create css and images directories and write all three static files",
            "commands": ["mkdir -p css images", GARDEN_VERIFY],
            "verification": GARDEN_VERIFY,
            "rollback": "rm -f index.html css/style.css images/flower-bg.svg && rmdir css images 2>/dev/null || true",
            "expected_files": ["index.html", "css/style.css", "images/flower-bg.svg"],
            "ops": [
                {"op": "write_file", "path": "index.html", "content": GARDEN_HTML},
                {"op": "write_file", "path": "css/style.css", "content": GARDEN_CSS},
                {
                    "op": "write_file",
                    "path": "images/flower-bg.svg",
                    "content": GARDEN_SVG,
                },
            ],
        }
    ]


def _garden_3step() -> List[Dict[str, Any]]:
    """3-step: mkdir + create HTML + create CSS/SVG."""
    return [
        {
            "step_number": 1,
            "description": "Create subdirectory structure",
            "commands": ["mkdir -p css images"],
            "verification": "node -e \"require('fs').existsSync('css') || process.exit(1)\"",
            "rollback": "rmdir css images 2>/dev/null || true",
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Write index.html landing page",
            "commands": [],
            "verification": "node -e \"require('fs').existsSync('index.html') || process.exit(1)\"",
            "rollback": "rm -f index.html",
            "expected_files": ["index.html"],
            "ops": [
                {"op": "write_file", "path": "index.html", "content": GARDEN_HTML},
            ],
        },
        {
            "step_number": 3,
            "description": "Write CSS stylesheet and SVG flower illustration, verify all files",
            "commands": [GARDEN_VERIFY],
            "verification": GARDEN_VERIFY,
            "rollback": "rm -f css/style.css images/flower-bg.svg",
            "expected_files": ["css/style.css", "images/flower-bg.svg"],
            "ops": [
                {"op": "write_file", "path": "css/style.css", "content": GARDEN_CSS},
                {
                    "op": "write_file",
                    "path": "images/flower-bg.svg",
                    "content": GARDEN_SVG,
                },
            ],
        },
    ]


def _garden_minimal_content_1step() -> List[Dict[str, Any]]:
    """1-step with bare-minimum file content to establish the floor."""
    return [
        {
            "step_number": 1,
            "description": "Create css and images directories and write static site files",
            "commands": ["mkdir -p css images", GARDEN_VERIFY],
            "verification": GARDEN_VERIFY,
            "rollback": "rm -f index.html css/style.css images/flower-bg.svg",
            "expected_files": ["index.html", "css/style.css", "images/flower-bg.svg"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "index.html",
                    "content": "<html><body><h1>Garden Story</h1></body></html>",
                },
                {
                    "op": "write_file",
                    "path": "css/style.css",
                    "content": "body { margin: 0; }",
                },
                {
                    "op": "write_file",
                    "path": "images/flower-bg.svg",
                    "content": '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="50" cy="50" r="40"/></svg>',
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMinimumValidPlanSize:
    """Characterize minimum validator-accepted plan size per fixture."""

    def test_tiny_money_1step_schema_valid(self):
        plan = _tiny_money_1step()
        result = ValidatorService.validate_plan_schema(plan)
        assert result["valid"], f"Schema errors: {result['errors']}"

    def test_tiny_money_1step_accepted(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "tiny_money_source_rewrite"
        )
        project_dir = fixture if fixture.exists() else tmp_path
        plan = _tiny_money_1step()
        status, m = validate_and_measure(
            plan,
            task_prompt=TINY_MONEY_TASK,
            execution_profile="medium_cli_single_file",
            project_dir=project_dir,
            label="tiny_money_1step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_tiny_money_2step_accepted(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "tiny_money_source_rewrite"
        )
        project_dir = fixture if fixture.exists() else tmp_path
        plan = _tiny_money_2step()
        status, m = validate_and_measure(
            plan,
            task_prompt=TINY_MONEY_TASK,
            execution_profile="medium_cli_single_file",
            project_dir=project_dir,
            label="tiny_money_2step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_stale_replace_write_1step_accepted(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "stale_replace_repair"
        )
        project_dir = fixture if fixture.exists() else tmp_path
        plan = _stale_replace_write_1step()
        status, m = validate_and_measure(
            plan,
            task_prompt=STALE_REPLACE_TASK,
            execution_profile="medium_cli_single_file",
            project_dir=project_dir,
            label="stale_replace_1step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_stale_replace_repair_1step_accepted(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "stale_replace_repair"
        )
        project_dir = fixture if fixture.exists() else tmp_path
        plan = _stale_replace_repair_1step(project_dir)
        status, m = validate_and_measure(
            plan,
            task_prompt=STALE_REPLACE_TASK,
            execution_profile="medium_cli_single_file",
            project_dir=project_dir,
            label="stale_replace_replace_in_file_1step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_garden_story_1step_accepted(self, tmp_path):
        plan = _garden_1step()
        status, m = validate_and_measure(
            plan,
            task_prompt=GARDEN_TASK,
            execution_profile="simple_static_site",
            project_dir=tmp_path,
            label="garden_story_task1_1step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_garden_story_minimal_content_1step_accepted(self, tmp_path):
        plan = _garden_minimal_content_1step()
        status, m = validate_and_measure(
            plan,
            task_prompt=GARDEN_TASK,
            execution_profile="simple_static_site",
            project_dir=tmp_path,
            label="garden_story_minimal_1step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"

    def test_garden_story_3step_accepted(self, tmp_path):
        plan = _garden_3step()
        status, m = validate_and_measure(
            plan,
            task_prompt=GARDEN_TASK,
            execution_profile="simple_static_site",
            project_dir=tmp_path,
            label="garden_story_3step",
        )
        assert status == "accepted", f"Expected accepted, got {status}"


class TestMinimumPlanMeasurements:
    """Measure and report plan sizes. Print detailed breakdowns."""

    @staticmethod
    def _report(label: str, status: str, m: Dict[str, Any]) -> None:
        print(f"\n{'=' * 60}")
        print(f"  {label}  [{status}]")
        print(f"{'=' * 60}")
        print(f"  Steps:             {m['step_count']}")
        print(f"  Compact chars:     {m['compact_chars']}")
        print(f"  Estimated tokens:  {m['estimated_tokens']}")
        print(f"  Structural chars:  {m['structural_overhead_chars']}")
        print(f"  Field breakdown (chars):")
        for f, c in sorted(m["per_field_chars"].items(), key=lambda x: -x[1]):
            pct = round(100 * c / m["compact_chars"])
            print(f"    {f:<20} {c:>5} chars  {pct:>3}%")

    def test_print_tiny_money_measurements(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "tiny_money_source_rewrite"
        )
        project_dir = fixture if fixture.exists() else tmp_path

        for label, plan in [
            ("tiny_money / 1-step / write_file", _tiny_money_1step()),
            ("tiny_money / 2-step / canonical", _tiny_money_2step()),
        ]:
            status, m = validate_and_measure(
                plan,
                task_prompt=TINY_MONEY_TASK,
                execution_profile="medium_cli_single_file",
                project_dir=project_dir,
                label=label,
            )
            self._report(label, status, m)

    def test_print_stale_replace_measurements(self, tmp_path):
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "stale_replace_repair"
        )
        project_dir = fixture if fixture.exists() else tmp_path

        for label, plan in [
            ("stale_replace / 1-step / write_file", _stale_replace_write_1step()),
            (
                "stale_replace / 1-step / replace_in_file",
                _stale_replace_repair_1step(project_dir),
            ),
        ]:
            status, m = validate_and_measure(
                plan,
                task_prompt=STALE_REPLACE_TASK,
                execution_profile="medium_cli_single_file",
                project_dir=project_dir,
                label=label,
            )
            self._report(label, status, m)

    def test_print_garden_story_measurements(self, tmp_path):
        for label, plan in [
            ("garden_story / 1-step / full-content", _garden_1step()),
            (
                "garden_story / 1-step / minimal-content",
                _garden_minimal_content_1step(),
            ),
            ("garden_story / 3-step / split-by-file", _garden_3step()),
        ]:
            status, m = validate_and_measure(
                plan,
                task_prompt=GARDEN_TASK,
                execution_profile="simple_static_site",
                project_dir=tmp_path,
                label=label,
            )
            self._report(label, status, m)

    def test_schema_overhead_ratio(self, tmp_path):
        """Measure what fraction of plan output is schema structure vs task content."""
        fixture = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "evals"
            / "fixtures"
            / "tiny_money_source_rewrite"
        )
        project_dir = fixture if fixture.exists() else tmp_path

        plan = _tiny_money_1step()
        m = measure_plan(plan)
        compact = _json_compact(plan)

        # JSON structural chars (brackets, braces, commas)
        struct_chars = m["structural_overhead_chars"]
        # Key overhead: all the field names ("step_number":, "description":, etc.)
        key_chars = sum(sum(_key_overhead(f) for f in step.keys()) for step in plan)
        # Content chars: what remains (actual semantic values)
        content_chars = m["compact_chars"] - struct_chars - key_chars

        print(f"\nSchema overhead breakdown (tiny_money 1-step):")
        print(f"  Total chars:       {m['compact_chars']}")
        print(
            f"  Structural chars:  {struct_chars} ({round(100 * struct_chars / m['compact_chars'])}%)"
        )
        print(
            f"  Key name chars:    {key_chars} ({round(100 * key_chars / m['compact_chars'])}%)"
        )
        print(
            f"  Schema overhead:   {struct_chars + key_chars} ({round(100 * (struct_chars + key_chars) / m['compact_chars'])}%)"
        )
        print(
            f"  Content chars:     {content_chars} ({round(100 * content_chars / m['compact_chars'])}%)"
        )

        # Schema overhead = structure + keys; content = values
        schema_overhead_pct = round(
            100 * (struct_chars + key_chars) / m["compact_chars"]
        )
        print(f"\n  -> Schema overhead is {schema_overhead_pct}% of total output")

        # Assert measurement completed (not a correctness assertion)
        assert m["compact_chars"] > 0

    def test_field_classification(self, tmp_path, capsys):
        """Classify fields by validator requirement type."""
        print("\nField classification:")
        print(
            "  validator-required (schema):    step_number, description, commands, verification, rollback, expected_files"
        )
        print("  planner-required (content):     ops.content (for write_file)")
        print(
            "  execution-required (runtime):   commands (runnable commands), ops.path, ops.op"
        )
        print(
            "  schema-only overhead:           field names, JSON punctuation, null values"
        )
        print()
        print(
            "  ops field:  optional by schema; execution-required by planner conventions"
        )
        print(
            "  rollback:   required by schema; often null (1 char value = 6 chars total)"
        )
        print("  verification: required by schema; duplicates commands for impl tasks")

        # Measure null/empty field costs across a real plan
        plan = _tiny_money_2step()
        for step in plan:
            sn = step["step_number"]
            fields_cost = {f: _field_chars(step, f) + _key_overhead(f) for f in step}
            print(f"\n  Step {sn} field costs:")
            for f, c in sorted(fields_cost.items(), key=lambda x: -x[1]):
                print(f"    {f:<20} {c:>5} chars")

        assert True
