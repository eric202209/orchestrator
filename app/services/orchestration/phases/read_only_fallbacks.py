"""Fallback plans for read-only orchestration stages."""

from __future__ import annotations

import json
from typing import Any

from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import READ_ONLY_WORKFLOW_STAGES


def _read_only_stage_fallback_plan(
    ctx: OrchestrationRunContext,
) -> list[dict[str, Any]] | None:
    if ctx.workflow_stage not in READ_ONLY_WORKFLOW_STAGES:
        return None

    script = (
        "import pathlib; "
        "files=[p for p in pathlib.Path('.').rglob('*') "
        "if p.is_file() and '.agent' not in p.parts]; "
        "print('\\n'.join(str(p) for p in files[:200]))"
    )
    command = "python -c " + json.dumps(script)
    stage_label = str(ctx.workflow_stage or "review").replace("_", " ")
    return [
        {
            "step_number": 1,
            "description": f"Inspect workspace for {stage_label} stage",
            "commands": [command],
            "verification": command,
            "rollback": "true",
            "expected_files": [],
        }
    ]
