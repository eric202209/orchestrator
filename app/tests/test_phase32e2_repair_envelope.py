import hashlib
import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.orchestration.planning import repair_prompts
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_prompts import (
    build_compact_stale_replace_repair_prompt,
    build_minimum_safe_stale_replace_repair_envelope,
)
from app.services.orchestration.planning.source_materialization import (
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    repair_projection_required_records,
    render_repair_source_materialization,
)


ATTEMPT6_TASK_DESCRIPTION = """Add a shared timezone-aware UTC helper at app/time_utils.py:

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

Update app/services/workspace/context_service.py so the existing exported_at field uses utc_now().isoformat() instead of datetime.utcnow().isoformat().

Add focused tests in app/tests/test_utc_now_helper.py proving the helper returns a timezone-aware UTC datetime.

Remove the now-unused local datetime import from context_service.py.

Modify exactly these three files:
* app/time_utils.py
* app/services/workspace/context_service.py
* app/tests/test_utc_now_helper.py

Do not migrate other datetime.utcnow() uses. Do not modify unrelated services, API endpoints, models, configuration, scheduling, queue, orchestration, provider, workspace, or recovery code."""

ATTEMPT6_REQUIRED_PATHS = (
    "app/time_utils.py",
    "app/services/workspace/context_service.py",
    "app/tests/test_utc_now_helper.py",
)
ATTEMPT6_WORKSPACE_IDENTITY = "/root/.orchestrator/runtime/tasks/12/252"
ATTEMPT6_CONTEXT_HASH = (
    "88073779716c402ebaf60582cc699ebd94c0405e3f29034ab7bd8a457881f0d7"
)
ATTEMPT6_CONTEXT_VERSION = "74:17444396:15720:1785867327907096722"
ATTEMPT6_SHORTEST_PRE_FAILURE_SHA256 = (
    "0bfbe966f29a162fcf38552d89f52586cbef9033c404610b616fa05bd10af598"
)


@pytest.fixture(autouse=True)
def _pin_floor_repair_budget(monkeypatch):
    """Phase 32N-1: pin this module to the floor repair-prompt budget.

    The effective budget is now derived from ``PLANNING_REPAIR_CONTEXT_TOKENS``.
    Every contract in this module was written against the 8,000-character floor
    and must keep exercising that bound regardless of what the local deployment
    declares, so the assertions below stay exactly as strong as they were.
    """

    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _attempt6_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Create the time_utils.py file with the utc_now function",
            "ops": [
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
            "commands": [],
            "verification": (
                "python -c \"import app.time_utils; print('Import successful'); "
                'print(app.time_utils.utc_now())"'
            ),
            "rollback": "rm -f app/time_utils.py",
            "expected_files": ["app/time_utils.py"],
        },
        {
            "step_number": 2,
            "description": (
                "Update context_service.py to use utc_now() instead of "
                "datetime.utcnow() and remove unused import"
            ),
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "app/services/workspace/context_service.py",
                    "old": (
                        "from datetime import datetime\n\n\n"
                        "def utc_now() -> datetime:\n"
                        "    return datetime.now(timezone.utc)\n"
                    ),
                    "new": (
                        "from datetime import datetime, timezone\n"
                        "from app.time_utils import utc_now\n\n\n"
                        "def utc_now() -> datetime:\n"
                        "    return datetime.now(timezone.utc)\n"
                    ),
                },
                {
                    "op": "replace_in_file",
                    "path": "app/services/workspace/context_service.py",
                    "old": '"exported_at": datetime.utcnow().isoformat(),',
                    "new": '"exported_at": utc_now().isoformat(),',
                },
            ],
            "commands": [],
            "verification": (
                'python -c "from app.services.workspace.context_service import *; '
                "import inspect; print('Context service import successful')\""
            ),
            "rollback": "rm -f app/services/workspace/context_service.py.bak",
            "expected_files": ["app/services/workspace/context_service.py"],
        },
        {
            "step_number": 3,
            "description": "Create test file for utc_now helper function",
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/tests/test_utc_now_helper.py",
                    "content": (
                        "import pytest\n"
                        "from datetime import datetime, timezone\n"
                        "from app.time_utils import utc_now\n\n"
                        "def test_utc_now_returns_timezone_aware_utc_datetime():\n"
                        '    """Test that utc_now returns a timezone-aware UTC '
                        'datetime"""\n'
                        "    dt = utc_now()\n"
                        "    assert isinstance(dt, datetime)\n"
                        "    assert dt.tzinfo == timezone.utc\n"
                        "    assert dt == datetime.now(timezone.utc)\n"
                    ),
                }
            ],
            "commands": [],
            "verification": "python -m pytest app/tests/test_utc_now_helper.py -v",
            "rollback": "rm -f app/tests/test_utc_now_helper.py",
            "expected_files": ["app/tests/test_utc_now_helper.py"],
        },
    ]


def _attempt6_malformed_output() -> str:
    output = json.dumps(_attempt6_plan(), indent=2)
    assert len(output) == 2530
    assert (
        hashlib.sha256(output.encode("utf-8")).hexdigest()
        == "a7b2f32d51a56815d50af75d9859094633b6822d405d4e775ca4f23420de6028"
    )
    return output


def _retained_excerpt(
    repository_root: Path,
    relative_path: str,
    *,
    start_byte: int,
    end_byte: int,
    truncated_before: bool,
    truncated_after: bool,
) -> str:
    source = (repository_root / relative_path).read_bytes()
    content = source[start_byte:end_byte].decode("utf-8")
    if truncated_before:
        content = "... [truncated]\n" + content
    if truncated_after:
        content += "\n... [truncated]"
    return content


def _record(relative_path: str, **overrides) -> MaterializedSourceFile:
    values = {
        "relative_path": relative_path,
        "workspace_identity": ATTEMPT6_WORKSPACE_IDENTITY,
        "content": None,
        "content_hash": None,
        "version_identity": None,
        "status": "source_omitted_by_explicit_bound",
        "truncated": False,
        "source_length": None,
        "source_length_chars": None,
        "included_prompt_length": 0,
        "expected": False,
        "creation_authorized": False,
        "omission_reason": "maximum_total_source_bytes",
        "priority": "P3",
        "selection_strategy": "omitted_total_budget",
        "full_source_bytes": None,
        "included_source_bytes": 0,
    }
    values.update(overrides)
    return MaterializedSourceFile(**values)


def _attempt6_materialization(repository_root: Path) -> PlannerSourceMaterialization:
    context_excerpt = _retained_excerpt(
        repository_root,
        "app/services/workspace/context_service.py",
        start_byte=12969,
        end_byte=14932,
        truncated_before=True,
        truncated_after=True,
    )
    assert len(context_excerpt) == 1995
    assert hashlib.sha256(context_excerpt.encode("utf-8")).hexdigest() == (
        ATTEMPT6_CONTEXT_HASH
    )
    openclaw_excerpt = _retained_excerpt(
        repository_root,
        "app/services/agents/openclaw_service.py",
        start_byte=103638,
        end_byte=105577,
        truncated_before=True,
        truncated_after=True,
    )
    router_excerpt = _retained_excerpt(
        repository_root,
        "app/api/v1/router.py",
        start_byte=0,
        end_byte=997,
        truncated_before=False,
        truncated_after=True,
    )
    files = (
        _record(
            "app/time_utils.py",
            status="new_file_authorized_for_creation",
            expected=True,
            creation_authorized=True,
            omission_reason=None,
            priority="P0",
            selection_strategy="new_file_no_source",
        ),
        _record(
            "app/services/workspace/context_service.py",
            content=context_excerpt,
            content_hash=ATTEMPT6_CONTEXT_HASH,
            version_identity=ATTEMPT6_CONTEXT_VERSION,
            status="existing_file_with_materialized_source",
            truncated=True,
            source_length=15720,
            source_length_chars=15664,
            included_prompt_length=1995,
            expected=True,
            omission_reason=None,
            priority="P0",
            selection_strategy="target_centered_exact_match",
            full_source_bytes=15720,
            included_source_bytes=1995,
            start_byte=12969,
            end_byte=14932,
            start_line=426,
            end_line=479,
            truncated_before=True,
            truncated_after=True,
            target_hint="isoformat()",
            target_hint_type="exact_call",
            target_hint_authority="task_description",
            target_hint_status="target_hint_matched",
            target_match_count=1,
            target_match_start=14017,
            target_match_end=14028,
            target_included=True,
        ),
        _record(
            "app/tests/test_utc_now_helper.py",
            status="new_file_authorized_for_creation",
            expected=True,
            creation_authorized=True,
            omission_reason=None,
            priority="P1",
            selection_strategy="new_file_no_source",
        ),
        _record(
            "app/services/agents/openclaw_service.py",
            content=openclaw_excerpt,
            content_hash=(
                "b01f25331fea9b540f938dad5d9e4c66eb7b7b0d74bb6a9ad269e531b08c0f30"
            ),
            version_identity="74:17444026:150026:1785867327819096609",
            status="existing_file_with_materialized_source",
            truncated=True,
            source_length=150026,
            source_length_chars=150016,
            included_prompt_length=1971,
            omission_reason=None,
            priority="P2",
            selection_strategy="target_centered_exact_match",
            full_source_bytes=150026,
            included_source_bytes=1971,
            start_byte=103638,
            end_byte=105577,
            start_line=2454,
            end_line=2514,
            truncated_before=True,
            truncated_after=True,
            target_hint="isoformat()",
            target_hint_type="exact_call",
            target_hint_authority="task_description",
            target_hint_status="target_hint_matched",
            target_match_count=4,
            target_match_start=104569,
            target_match_end=104580,
            target_included=True,
        ),
        _record(
            "app/api/v1/router.py",
            content=router_excerpt,
            content_hash=(
                "70ed574e734134730eb3e54c73622e67b6424f79b7d7fe415207bedad6de49a7"
            ),
            version_identity="74:17443993:5164:1785867327812096600",
            status="existing_file_with_materialized_source",
            truncated=True,
            source_length=5164,
            source_length_chars=5156,
            included_prompt_length=1013,
            omission_reason=None,
            priority="P3",
            selection_strategy="head_fallback_no_target",
            full_source_bytes=5164,
            included_source_bytes=1013,
            start_byte=0,
            end_byte=997,
            start_line=1,
            end_line=31,
            truncated_after=True,
            target_hint_status="target_hint_not_found",
        ),
        *(
            _record(path)
            for path in (
                "app/config.py",
                "app/database.py",
                "app/dependencies.py",
                "app/models.py",
                "app/services/agents/backend_lane_snapshot.py",
                "app/services/auth/rate_limit.py",
            )
        ),
    )
    return PlannerSourceMaterialization(
        workspace_identity=ATTEMPT6_WORKSPACE_IDENTITY,
        files=files,
        maximum_files=25,
        maximum_bytes_per_file=2000,
        maximum_total_source_bytes=5000,
        materialized_source_bytes=4979,
    )


def _attempt6_rejection_reasons(repository_root: Path) -> list[str]:
    return [
        "replace_in_file old text not found in workspace in steps [2]",
        *PlannerService.stale_replace_repair_hints(_attempt6_plan(), repository_root),
    ]


def _build_attempt6(repository_root: Path):
    return build_compact_stale_replace_repair_prompt(
        task_description=ATTEMPT6_TASK_DESCRIPTION,
        malformed_output=_attempt6_malformed_output(),
        project_dir=repository_root,
        rejection_reasons=_attempt6_rejection_reasons(repository_root),
        prompt_profile="local_qwen_json_array",
        apply_prompt_profile=PlannerService.apply_prompt_profile,
        source_materialization=_attempt6_materialization(repository_root),
    )


def _minimum_attempt6_envelope(repository_root: Path):
    return build_minimum_safe_stale_replace_repair_envelope(
        task_description=ATTEMPT6_TASK_DESCRIPTION,
        malformed_output=_attempt6_malformed_output(),
        rejection_reasons=_attempt6_rejection_reasons(repository_root),
        prompt_profile="local_qwen_json_array",
        apply_prompt_profile=PlannerService.apply_prompt_profile,
        source_materialization=_attempt6_materialization(repository_root),
    )


def test_attempt6_minimum_safe_repair_envelope_fits(repository_root):
    result = _build_attempt6(repository_root)

    assert isinstance(result, str)
    assert len(result) <= 8000


def test_attempt6_projection_reaches_real_level_four(repository_root, monkeypatch):
    levels = []
    original = repair_prompts.render_repair_source_materialization

    def record_level(*args, **kwargs):
        levels.append(kwargs.get("compaction_level", 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        repair_prompts, "render_repair_source_materialization", record_level
    )

    _build_attempt6(repository_root)

    assert levels[:4] == [0, 1, 2, 3]


def test_level_four_contains_only_required_repair_records(repository_root):
    materialization = _attempt6_materialization(repository_root)
    required = repair_projection_required_records(
        materialization, ATTEMPT6_REQUIRED_PATHS
    )
    projected = render_repair_source_materialization(
        materialization,
        rejected_paths=ATTEMPT6_REQUIRED_PATHS,
        compaction_level=4,
    )

    assert [(record.relative_path, priority) for record, priority in required] == [
        ("app/time_utils.py", "R0"),
        ("app/services/workspace/context_service.py", "R0"),
        ("app/tests/test_utc_now_helper.py", "R0"),
    ]
    assert "app/services/agents/openclaw_service.py" not in projected
    assert "lower-priority supporting source records omitted" not in projected
    assert len(projected) == 3237


def test_attempt6_failure_is_rendered_once(repository_root):
    result = _build_attempt6(repository_root)

    assert isinstance(result, str)
    assert result.count("old text") >= 1


def test_attempt6_source_provenance_is_rendered_once(repository_root):
    result = _build_attempt6(repository_root)

    assert isinstance(result, str)
    assert result.count("## CURRENT SOURCE MATERIALIZATION") == 1
    assert ATTEMPT6_CONTEXT_HASH not in result
    assert ATTEMPT6_CONTEXT_VERSION not in result
    assert "visible_lines:" not in result
    assert "selector internals" in result
    assert result.count("status: new_file_authorized_for_creation") == 2


def test_attempt6_safe_envelope_retains_complete_plan_authority(repository_root):
    result = _build_attempt6(repository_root)

    assert isinstance(result, str)
    assert "datetime.now(timezone.utc)" in result
    assert '"exported_at": datetime.utcnow().isoformat(),' in result
    assert "old text" in result
    assert "selector internals" in result
    assert "Never reconstruct or overwrite a whole existing file" not in result
    assert "description, commands, verification, expected_files" in result
    assert "optional step_number, rollback, and ops" in result


def test_attempt6_section_accounting_exactly_reconciles_and_is_deterministic(
    repository_root,
):
    first = _minimum_attempt6_envelope(repository_root)
    second = _minimum_attempt6_envelope(repository_root)

    assert first is not None
    assert second is not None
    assert first == second
    assert (
        hashlib.sha256(first.prompt.encode("utf-8")).hexdigest()
        == hashlib.sha256(second.prompt.encode("utf-8")).hexdigest()
    )
    assert sum(section.character_count for section in first.sections) == len(
        first.prompt
    )
    assert first.sections[0].start_offset == 0
    assert first.sections[-1].end_offset == len(first.prompt)
    assert all(
        left.end_offset == right.start_offset
        for left, right in zip(first.sections, first.sections[1:])
    )
    assert all(section.required for section in first.sections)
    assert {section.section_name for section in first.sections} >= {
        "repair_role_instruction_header",
        "task_title_and_description",
        "accepted_task_scope_and_expected_files",
        "validator_findings_and_immediate_repair_issues",
        "failed_plan_operations_and_prior_plan_text",
        "source_materialization_preamble",
        "R0_source_record_metadata",
        "R0_source_excerpt",
        "new_file_creation_authorization_records",
        "operation_safety_and_replacement_authorization_rules",
        "general_planning_no_progress_and_arbitration_guidance",
        "json_schema_and_output_format",
        "provider_adaptation_instructions",
    }


def test_attempt6_deployed_projection_hash_is_stable(repository_root, monkeypatch):
    candidates = []
    original = repair_prompts._apply_profile

    def capture(prompt, prompt_profile, apply_prompt_profile):
        rendered = original(prompt, prompt_profile, apply_prompt_profile)
        candidates.append(rendered)
        return rendered

    monkeypatch.setattr(repair_prompts, "_apply_profile", capture)

    result = _build_attempt6(repository_root)

    assert isinstance(result, str)
    deployed_candidates = [
        candidate
        for candidate in candidates
        if "Stale replace repair mode." in candidate
    ]
    shortest = min(deployed_candidates, key=len)
    assert len(shortest) <= 8000
    assert ATTEMPT6_CONTEXT_HASH not in shortest
    assert ATTEMPT6_CONTEXT_VERSION not in shortest
    assert "visible_lines:" not in shortest
    legacy_projection = render_repair_source_materialization(
        _attempt6_materialization(repository_root),
        rejected_paths=ATTEMPT6_REQUIRED_PATHS,
        compaction_level=3,
    )
    assert len(legacy_projection) == 3628
    assert len(legacy_projection) - 1995 == 1633
