"""P42-B1: measured defect detection, not source-text adequacy heuristics.

Synthetic controls model the documented Phase 41 edge-separator defect;
these are not reconstructed Qwen artifacts. No runtime or database is used.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
)
from app.services.orchestration.validation.validator import ValidatorService


BUGGY = """import re


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug
"""
CORRECTED = BUGGY.replace("return slug\n", 'return slug.strip("-")\n')
WEAK = """from slug import slugify


def test_no_edge_hyphens():
    assert slugify("  Hello World  ") == "hello-world"
"""
STRONG = WEAK.replace('"  Hello World  "', '"!Hello World!"')


@pytest.mark.parametrize(
    "source,test_body,expected_code",
    [
        pytest.param(BUGGY, WEAK, 0, id="weak-buggy-passes"),
        pytest.param(CORRECTED, WEAK, 0, id="weak-corrected-passes"),
        pytest.param(BUGGY, STRONG, 1, id="strong-buggy-fails"),
        pytest.param(CORRECTED, STRONG, 0, id="strong-corrected-passes"),
    ],
)
def test_defect_detection_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    test_body: str,
    expected_code: int,
):
    # A fresh directory and process for each cell avoids import/bytecode reuse.
    for name, content in (("slug.py", source), ("test_slug.py", test_body)):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o666)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _root: sys.executable,
    )
    command = "python -m pytest -q -p no:cacheprovider test_slug.py"
    # Both use the identical admitted command and test name. This classifier
    # measures command form, and must not be treated as an adequacy oracle.
    assert ValidatorService._verification_is_weak(command) is False
    result = execute_verification_command(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=30,
        include_evidence=True,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert result["returncode"] == expected_code, result
    assert result["success"] is (expected_code == 0), result
    evidence = result["verification_evidence"]
    assert evidence["command_execution_status"] == "completed"
    assert evidence["execution_kind"] == "subprocess"
    assert evidence["process_exit_code"] == expected_code
    assert evidence["verification_status"] == (
        "passed" if expected_code == 0 else "failed"
    )
    if expected_code:
        assert "AssertionError" in result["output"]
        assert "-hello-world-" in result["output"]
        assert "1 failed" in result["output"]
    else:
        assert "1 passed" in result["output"]
