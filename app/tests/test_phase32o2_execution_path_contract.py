from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.orchestration.context import assembly
from app.services.orchestration.diagnostics.debug_feedback import (
    build_bounded_debug_repair_prompt,
    build_debug_feedback_envelope,
    normalize_bounded_debug_repair_payload_detailed,
    parse_bounded_debug_repair_output_strict,
)
from app.services.orchestration.execution.execution_flow import assess_step_execution
from app.services.orchestration.execution.executor import (
    ExecutorService,
    WorkspaceProductPathError,
)
from app.services.orchestration.execution.runtime import restore_workspace_after_abort
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.validation.validator import ValidatorService


ATTEMPT10_RELATIVE_PATH = "app/services/workspace/context_service.py"


def _execution_context(runtime_workspace: Path) -> SimpleNamespace:
    state = OrchestrationState(
        session_id="124",
        task_description="Attempt 10 provider-free execution replay",
        project_name="Orchestrator",
        project_context="Retained Attempt 10 context",
        task_id=177,
    )
    state._project_dir_override = str(runtime_workspace)
    return SimpleNamespace(
        db=None,
        prompt="Update the workspace context export timestamp",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=state,
    )


def _attempt10_tool_failure(runtime_workspace: Path) -> str:
    doubled = runtime_workspace / "256" / ATTEMPT10_RELATIVE_PATH
    return (
        "[tools] read failed: ENOENT: no such file or directory, access "
        f'\'{doubled}\' raw_params={{"path":"256/{ATTEMPT10_RELATIVE_PATH}"}}'
    )


def test_execution_prompt_does_not_apply_bound_workspace_name_to_file_tools(
    tmp_path, monkeypatch
):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)
    monkeypatch.setattr(
        assembly,
        "render_workspace_path_for_prompt",
        lambda *args, **kwargs: "256",
    )

    prompt = assembly.assemble_execution_prompt(
        _execution_context(runtime_workspace),
        {
            "description": "Verify the retained Step 2 operations",
            "commands": [
                "python -m py_compile app/services/workspace/context_service.py"
            ],
            "verification": "python -m py_compile app/services/workspace/context_service.py",
            "rollback": None,
            "expected_files": [ATTEMPT10_RELATIVE_PATH],
        },
    )

    assert "must use an absolute canonical path" not in prompt
    assert (
        "file-read or file-write tool call must use a workspace-relative path" in prompt
    )
    assert "256/app/services/workspace/context_service.py" not in prompt


def test_attempt10_verification_hint_does_not_repeat_doubled_root(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)

    hints = ExecutorService.tool_failure_correction_hints(
        [_attempt10_tool_failure(runtime_workspace)], runtime_workspace
    )
    rendered = " ".join(hints)

    assert ATTEMPT10_RELATIVE_PATH in rendered
    assert str(runtime_workspace / "256") not in rendered
    assert str(runtime_workspace) not in rendered


def test_attempt10_correct_relative_path_is_used_in_correction_guidance(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)

    hints = ExecutorService.tool_failure_correction_hints(
        [_attempt10_tool_failure(runtime_workspace)], runtime_workspace
    )

    assert hints == [
        "File-tool paths are workspace-relative. Retry the read/write using "
        f"`{ATTEMPT10_RELATIVE_PATH}` from the current Runtime Workspace."
    ]


def test_absolute_structured_operation_path_fails_closed(tmp_path):
    target = tmp_path / "app" / "demo.py"
    result = ExecutorService.execute_file_ops(
        tmp_path,
        [{"op": "write_file", "path": str(target), "content": "VALUE = 1\n"}],
    )

    assert result["success"] is False
    assert "absolute" in result["output"]
    assert not target.exists()


def test_debug_repair_missing_command_reaches_strict_failure():
    result = normalize_bounded_debug_repair_payload_detailed(
        [{"title": "Retry verification", "verification_command": "python -m pytest -q"}]
    )

    assert result.payload is None
    assert result.rejection_reason == "missing_command"


def test_bounded_debug_repair_rejects_legacy_object_schema():
    result = normalize_bounded_debug_repair_payload_detailed(
        {
            "fix_type": "command_fix",
            "analysis": "Retry the existing verifier",
            "fix": "python -m pytest -q",
            "verification": "python -m pytest -q",
        }
    )

    assert result.payload is None
    assert result.rejection_reason == "unsupported_shape"


def test_completion_refuses_provider_success_with_enoent_tool_log(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)
    log = SimpleNamespace(message=_attempt10_tool_failure(runtime_workspace))
    query = MagicMock()
    query.filter.return_value.order_by.return_value.all.return_value = [log]
    db = MagicMock()
    db.query.return_value = query

    assessment = assess_step_execution(
        db=db,
        session_id=124,
        task_id=177,
        project_dir=runtime_workspace,
        step={
            "commands": [
                "python -m py_compile app/services/workspace/context_service.py"
            ],
            "ops": [],
            "verification": "",
            "expected_files": [],
        },
        step_result={"status": "completed", "output": "success"},
        step_started_at=datetime.now(UTC),
        validation_profile="mutation",
    )

    assert assessment.step_status == "failed"
    assert "task logs contain tool failures" in assessment.error_message


def test_fail_closed_restore_preserves_pre_run_snapshot_contract(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)
    task_service = MagicMock()
    task_service.restore_workspace_snapshot.return_value = {
        "restored": True,
        "file_count": 8222,
    }
    project = SimpleNamespace(id=12)

    result = restore_workspace_after_abort(
        task_service,
        project,
        177,
        runtime_workspace,
        task_execution_id=256,
        preserve_project_root_rules=True,
    )

    assert result == {"restored": True, "file_count": 8222}
    task_service.restore_workspace_snapshot.assert_called_once_with(
        project,
        runtime_workspace,
        snapshot_key="task-177-execution-256-pre-run",
        preserve_project_root_rules=True,
        skip_lock=False,
        snapshot_root=runtime_workspace,
    )


def test_workspace_relative_path_resolves_exactly_once_and_normalizes_separators(
    tmp_path,
):
    resolution = ExecutorService.resolve_workspace_product_path(
        tmp_path, r"app\services\demo.py"
    )

    assert resolution.relative_path == "app/services/demo.py"
    assert resolution.resolved_path == tmp_path / "app" / "services" / "demo.py"


def test_repeated_absolute_resolution_is_explicitly_rejected(tmp_path):
    first = ExecutorService.resolve_workspace_product_path(tmp_path, "app/demo.py")

    try:
        ExecutorService.resolve_workspace_product_path(
            tmp_path, str(first.resolved_path)
        )
    except WorkspaceProductPathError as exc:
        assert exc.code == "absolute_path_rejected"
    else:
        raise AssertionError("already-resolved absolute product path was admitted")


def test_workspace_path_authority_rejects_empty_traversal_and_escape(tmp_path):
    for raw_path, expected_code in (
        ("", "empty_path"),
        ("../outside.py", "traversal_rejected"),
        ("app/../../outside.py", "traversal_rejected"),
    ):
        try:
            ExecutorService.resolve_workspace_product_path(tmp_path, raw_path)
        except WorkspaceProductPathError as exc:
            assert exc.code == expected_code
        else:
            raise AssertionError(f"unsafe product path admitted: {raw_path!r}")


def test_workspace_path_authority_rejects_doubled_task_execution_segment(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)

    try:
        ExecutorService.resolve_workspace_product_path(
            runtime_workspace, f"256/{ATTEMPT10_RELATIVE_PATH}"
        )
    except WorkspaceProductPathError as exc:
        assert exc.code == "duplicated_task_execution_segment"
    else:
        raise AssertionError("doubled TaskExecution segment was admitted")


def test_attempt10_path_diagnostic_separates_relative_internal_and_raw_paths(
    tmp_path,
):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)
    malformed = str(runtime_workspace / "256" / ATTEMPT10_RELATIVE_PATH)

    diagnostic = ExecutorService.tool_failure_path_diagnostic(
        _attempt10_tool_failure(runtime_workspace), runtime_workspace
    )

    assert diagnostic == {
        "requested_relative_path": ATTEMPT10_RELATIVE_PATH,
        "resolved_internal_path": str(runtime_workspace / ATTEMPT10_RELATIVE_PATH),
        "provider_reported_path": malformed,
        "path_resolution_failure_code": "duplicated_task_execution_segment",
    }


def test_correction_prompt_keeps_raw_malformed_path_diagnostic_only(tmp_path):
    runtime_workspace = tmp_path / "runtime" / "tasks" / "12" / "256"
    runtime_workspace.mkdir(parents=True)
    malformed = str(runtime_workspace / "256" / ATTEMPT10_RELATIVE_PATH)
    envelope = build_debug_feedback_envelope(
        task_execution_id=256,
        task_id=177,
        step_index=2,
        failure_phase="execution",
        failed_command="python -m py_compile app/services/workspace/context_service.py",
        return_code=None,
        stdout=f"provider reported {malformed}",
        stderr=_attempt10_tool_failure(runtime_workspace),
        changed_files=[ATTEMPT10_RELATIVE_PATH],
        expected_files=[ATTEMPT10_RELATIVE_PATH],
        workspace_path=runtime_workspace,
    )

    prompt = build_bounded_debug_repair_prompt(envelope)

    assert ATTEMPT10_RELATIVE_PATH in prompt
    assert malformed not in prompt
    assert str(runtime_workspace) not in prompt


def test_strict_debug_repair_accepts_canonical_command_shape():
    result = parse_bounded_debug_repair_output_strict(
        json.dumps(
            [
                {
                    "title": "Retry focused verification",
                    "command": "python -m pytest -q app/tests/test_demo.py",
                    "verification_command": "python -m pytest -q app/tests/test_demo.py",
                }
            ]
        )
    )

    assert result.rejection_reason is None
    assert result.payload["fix_type"] == "command_fix"
    assert result.payload["fix"] == "python -m pytest -q app/tests/test_demo.py"


def test_strict_debug_repair_rejects_empty_command_extra_prose_and_extra_schema():
    cases = (
        (
            '[{"title":"Retry","command":"","verification_command":"python -m pytest -q"}]',
            "missing_command",
        ),
        (
            'Here is the fix: [{"title":"Retry","command":"python -m pytest -q","verification_command":"python -m pytest -q"}]',
            "json_parse_failed",
        ),
        (
            '[{"title":"Retry","command":"python -m pytest -q","verification_command":"python -m pytest -q","reason":"extra"}]',
            "unsupported_schema",
        ),
        (
            '[{"title":"Retry","command":"curl https://example.invalid","verification_command":"python -m pytest -q"}]',
            "non_runnable_command",
        ),
    )

    for raw_output, expected_reason in cases:
        result = parse_bounded_debug_repair_output_strict(raw_output)
        assert result.payload is None
        assert result.rejection_reason == expected_reason


def _provider_free_attempt10_replay(runtime_workspace: Path) -> dict[str, object]:
    canonical_context = Path("app/services/workspace/context_service.py")
    context_target = runtime_workspace / ATTEMPT10_RELATIVE_PATH
    context_target.parent.mkdir(parents=True)
    shutil.copy2(canonical_context, context_target)
    canonical_before = hashlib.sha256(canonical_context.read_bytes()).hexdigest()

    step1 = ExecutorService.execute_file_ops(
        runtime_workspace,
        [
            {
                "op": "write_file",
                "path": "app/time_utils.py",
                "content": (
                    "from datetime import datetime, timezone\n\n"
                    "def utc_now() -> datetime:\n"
                    "    return datetime.now(timezone.utc)\n"
                ),
            }
        ],
    )
    step2_ops = [
        {
            "op": "replace_in_file",
            "path": ATTEMPT10_RELATIVE_PATH,
            "old": (
                "import json\nimport logging\nfrom datetime import datetime\n"
                "from typing import Optional, Dict, Any, List\n\n"
                "from sqlalchemy.orm import Session as DBSession\n"
                "from sqlalchemy import func\n\nfrom app.models import (\n"
                "    SessionState,\n    ConversationHistory,\n"
                "    TaskCheckpoint,\n)"
            ),
            "new": (
                "import json\nimport logging\nfrom datetime import datetime\n"
                "from typing import Optional, Dict, Any, List\n\n"
                "from sqlalchemy.orm import Session as DBSession\n"
                "from sqlalchemy import func\n\nfrom app.models import (\n"
                "    SessionState,\n    ConversationHistory,\n"
                "    TaskCheckpoint,\n)\nfrom app.time_utils import utc_now"
            ),
        },
        {
            "op": "replace_in_file",
            "path": ATTEMPT10_RELATIVE_PATH,
            "old": '"exported_at": datetime.utcnow().isoformat(),',
            "new": '"exported_at": utc_now().isoformat(),',
        },
        {
            "op": "replace_in_file",
            "path": ATTEMPT10_RELATIVE_PATH,
            "old": "from datetime import datetime",
            "new": "",
        },
    ]
    step2 = ExecutorService.execute_file_ops(runtime_workspace, step2_ops)
    provider_resolution = ExecutorService.resolve_workspace_product_path(
        runtime_workspace, ATTEMPT10_RELATIVE_PATH
    )
    provider_content = provider_resolution.resolved_path.read_text(encoding="utf-8")
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = (
        []
    )
    completion = assess_step_execution(
        db=db,
        session_id=124,
        task_id=177,
        project_dir=runtime_workspace,
        step={
            "commands": [],
            "ops": step2_ops,
            "verification": "python -m py_compile app/services/workspace/context_service.py",
            "expected_files": [ATTEMPT10_RELATIVE_PATH],
        },
        step_result={
            "status": "completed",
            "output": step2["output"],
            "files_changed": step2["files_changed"],
        },
        step_started_at=datetime.now(UTC),
        validation_profile="mutation",
    )
    step3_ops = [
        {
            "op": "write_file",
            "path": "app/tests/test_utc_now_helper.py",
            "content": (
                "from datetime import datetime, timezone\n\n"
                "from app.time_utils import utc_now\n\n\n"
                "def test_utc_now_is_timezone_aware():\n"
                "    result = utc_now()\n"
                "    assert result.tzinfo is not None\n\n\n"
                "def test_utc_now_has_utc_offset():\n"
                "    assert utc_now().utcoffset().total_seconds() == 0\n\n\n"
                "def test_utc_now_is_close_to_current_time():\n"
                "    assert abs((datetime.now(timezone.utc) - utc_now()).total_seconds()) < 5\n"
            ),
        }
    ]
    step3 = ExecutorService.execute_file_ops(runtime_workspace, step3_ops)
    replay_plan = [
        {
            "step_number": 1,
            "description": "Create utc_now helper",
            "commands": [],
            "ops": [
                {"op": "write_file", "path": "app/time_utils.py", "content": "retained"}
            ],
            "verification": "python -m py_compile app/time_utils.py",
            "expected_files": ["app/time_utils.py"],
        },
        {
            "step_number": 2,
            "description": "Update context export timestamp",
            "commands": [],
            "ops": step2_ops,
            "verification": "python -m py_compile app/services/workspace/context_service.py",
            "expected_files": [ATTEMPT10_RELATIVE_PATH],
        },
        {
            "step_number": 3,
            "description": "Create utc_now tests",
            "commands": [],
            "ops": step3_ops,
            "verification": "python -m pytest -q app/tests/test_utc_now_helper.py",
            "expected_files": ["app/tests/test_utc_now_helper.py"],
        },
    ]
    candidate_validation = ValidatorService.validate_task_completion(
        project_dir=runtime_workspace,
        plan=replay_plan,
        task_prompt="Add utc_now helper, migrate context export, and add tests.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 3,
            "reported_changed_files": [
                "app/time_utils.py",
                ATTEMPT10_RELATIVE_PATH,
                "app/tests/test_utc_now_helper.py",
            ],
        },
    )
    candidate_hashes = {
        path: hashlib.sha256((runtime_workspace / path).read_bytes()).hexdigest()
        for path in (
            "app/time_utils.py",
            ATTEMPT10_RELATIVE_PATH,
            "app/tests/test_utc_now_helper.py",
        )
    }
    canonical_after = hashlib.sha256(canonical_context.read_bytes()).hexdigest()
    return {
        "step1_success": step1["success"],
        "step2_success": step2["success"],
        "step2_operation_count": len(step2_ops),
        "step3_success": step3["success"],
        "provider_relative_path": provider_resolution.relative_path,
        "provider_read_succeeded": "from app.time_utils import utc_now"
        in provider_content,
        "doubled_root_present": "/256/256/" in str(provider_resolution.resolved_path),
        "completion_status": completion.step_status,
        "completion_error": completion.error_message,
        "debug_repair_required": completion.step_status != "success",
        "candidate_diff_retained": sorted(candidate_hashes),
        "candidate_validation_entered": candidate_validation.stage == "task_completion",
        "candidate_validation_status": candidate_validation.status,
        "candidate_hashes": candidate_hashes,
        "canonical_unchanged": canonical_before == canonical_after,
    }


def test_attempt10_provider_free_replay_is_byte_deterministic(tmp_path):
    first = _provider_free_attempt10_replay(tmp_path / "first" / "tasks" / "12" / "256")
    second = _provider_free_attempt10_replay(
        tmp_path / "second" / "tasks" / "12" / "256"
    )

    first_bytes = json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
    second_bytes = json.dumps(second, sort_keys=True, separators=(",", ":")).encode()
    assert first_bytes == second_bytes
    assert first == {
        "step1_success": True,
        "step2_success": True,
        "step2_operation_count": 3,
        "step3_success": True,
        "provider_relative_path": ATTEMPT10_RELATIVE_PATH,
        "provider_read_succeeded": True,
        "doubled_root_present": False,
        "completion_status": "success",
        "completion_error": "",
        "debug_repair_required": False,
        "candidate_diff_retained": [
            ATTEMPT10_RELATIVE_PATH,
            "app/tests/test_utc_now_helper.py",
            "app/time_utils.py",
        ],
        "candidate_validation_entered": True,
        "candidate_validation_status": first["candidate_validation_status"],
        "candidate_hashes": first["candidate_hashes"],
        "canonical_unchanged": True,
    }
