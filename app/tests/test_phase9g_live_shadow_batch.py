from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.services.workspace import project_isolation_service


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "phase9g_live_shadow_batch.py"
)
SPEC = importlib.util.spec_from_file_location("phase9g_live_shadow_batch", SCRIPT_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_batch_workspace_root_uses_configured_openclaw_root(monkeypatch, tmp_path):
    workspace_root = tmp_path / "configured-openclaw-root"
    monkeypatch.setattr(
        module,
        "get_effective_workspace_root",
        lambda db=None: workspace_root,
    )

    batch_root = module._resolve_batch_workspace_root("batch-1", db=object())

    assert batch_root == workspace_root / ".openclaw-workspaces" / "batch-1"


def test_stored_project_workspace_path_is_root_relative(monkeypatch, tmp_path):
    workspace_root = tmp_path / "configured-openclaw-root"
    project_workspace = workspace_root / ".openclaw-workspaces" / "batch-1" / "01-docs"
    monkeypatch.setattr(
        project_isolation_service,
        "get_effective_workspace_root",
        lambda db=None: workspace_root,
    )

    stored_path = module._stored_project_workspace_path(
        project_workspace,
        project_name="batch-1-01-docs",
        db=object(),
    )

    assert stored_path == ".openclaw-workspaces/batch-1/01-docs"


def test_batch_summary_counts_only_current_batch_records():
    results = [
        {
            "terminal": {
                "task_execution_id": 201,
                "status": "done",
            }
        },
        {
            "terminal": {
                "task_execution_id": 202,
                "status": "failed",
            }
        },
    ]
    planning_report = {
        "records": [
            {
                "task_execution_id": 201,
                "shadow_warning_rule_ids": [],
            },
            {
                "task_execution_id": 202,
                "shadow_warning_rule_ids": [
                    "model_behavior.shell_quoting_patch",
                    "model_behavior.command_length_prompt_patch",
                ],
            },
            {
                "task_execution_id": 199,
                "shadow_warning_rule_ids": [
                    "model_behavior.shell_quoting_patch",
                ],
            },
        ],
    }

    summary = module._batch_summary(results, planning_report)

    assert summary == {
        "requested_count": 2,
        "task_execution_ids": [201, 202],
        "status_counts": {"done": 1, "failed": 1},
        "shadow_warning_rule_counts": {
            "model_behavior.command_length_prompt_patch": 1,
            "model_behavior.shell_quoting_patch": 1,
        },
        "contract_records_found": 2,
    }
