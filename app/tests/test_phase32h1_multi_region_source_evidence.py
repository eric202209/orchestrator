"""Phase 32H-1 multi-region source evidence and version-fenced staleness tests.

Two separate authorities are proved here:

* system-side deterministic stale verification may consult the complete current
  file, but only while its captured version identity still holds;
* model-visible source stays bounded, and an expected editable Python file may
  expose one target-centred span plus one bounded structural head span.
"""

import json
import shutil
from pathlib import Path

import pytest

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    MAX_SOURCE_CONTENT_TOTAL_CHARS,
    SELECTION_TARGET_EXACT,
    SELECTION_TARGET_WITH_STRUCTURAL_HEAD,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    SPAN_PRIMARY_TARGET,
    SPAN_STRUCTURAL_HEAD,
    materialize_planner_source_context,
)
from app.services.orchestration.planning.source_operation_verification import (
    FAILURE_MISSING_MATERIALIZATION,
    FAILURE_STALE_OLD_TEXT,
    FAILURE_VERSION_CHANGED,
    SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION,
    SOURCE_EVIDENCE_UNVERIFIED,
    SOURCE_EVIDENCE_VISIBLE_IN_SPAN,
    verify_replace_in_file,
)
from app.services.orchestration.validation.validator import (
    ValidatorService,
    _source_operation_contract_issues,
)


REFERENCE_TARGET = "app/services/workspace/context_service.py"

# The retained Phase 32 reference task.  It is the authority for both required
# regions: the module-head import and the call site near line 452.
REFERENCE_TASK = """Add a shared timezone-aware UTC helper at app/time_utils.py:

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

Update app/services/workspace/context_service.py so the existing exported_at \
field uses utc_now().isoformat() instead of datetime.utcnow().isoformat().

Add focused tests in app/tests/test_utc_now_helper.py proving the helper \
returns a timezone-aware UTC datetime.

Remove the now-unused local datetime import from context_service.py.

Modify exactly these three files:
* app/time_utils.py
* app/services/workspace/context_service.py
* app/tests/test_utc_now_helper.py

Do not migrate other datetime.utcnow() uses. Do not modify unrelated services, \
API endpoints, models, configuration, scheduling, queue, orchestration, \
provider, workspace, or recovery code."""

REFERENCE_EXPECTED = [
    REFERENCE_TARGET,
    "app/time_utils.py",
    "app/tests/test_utc_now_helper.py",
]

# No import wording: this task must never request a structural head span, so it
# isolates the version-fenced full-file authority.
CALL_SITE_ONLY_TASK = (
    "Replace the deprecated `datetime.utcnow()` call in pkg/mod.py with "
    "a shared `utc_now()` helper."
)

HEAD_IMPORT_LINE = "from datetime import datetime\n"
CALL_SITE_TEXT = "datetime.utcnow().isoformat()"


def _synthetic_module_text(filler_functions: int = 40) -> str:
    head = (
        '"""Synthetic module."""\n'
        "\n"
        "import json\n"
        f"{HEAD_IMPORT_LINE}"
        "from typing import Any\n"
        "\n"
        "\n"
    )
    filler = "".join(
        f"def filler_{index}(value: Any) -> Any:\n"
        f'    """Filler {index}."""\n'
        f"    return json.dumps({{'index': {index}, 'value': value}})\n"
        "\n"
        "\n"
        for index in range(filler_functions)
    )
    tail = "def exported_at() -> str:\n" f"    return {CALL_SITE_TEXT}\n"
    return head + filler + tail


@pytest.fixture
def synthetic_workspace(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text(_synthetic_module_text(), encoding="utf-8")
    return tmp_path


def _materialize(project_dir, task, expected, supporting=()):
    return materialize_planner_source_context(
        project_dir,
        task_description=task,
        expected_paths=expected,
        supporting_paths=list(supporting),
    )


def _replace_step(path, old, new, *, step_number=1):
    return {
        "step_number": step_number,
        "description": f"Apply the grounded replacement in {path}",
        "commands": [],
        "ops": [{"op": "replace_in_file", "path": path, "old": old, "new": new}],
        "verification": f"python3 -m py_compile {path}",
        "rollback": None,
        "expected_files": [path],
    }


# --- Case 1 — correct out-of-window import replacement --------------------


def test_case1_out_of_window_import_replacement_is_version_fenced_accepted(
    synthetic_workspace,
):
    materialization = _materialize(
        synthetic_workspace, CALL_SITE_ONLY_TASK, ["pkg/mod.py"]
    )
    record = materialization.file_map()["pkg/mod.py"]
    source = (synthetic_workspace / "pkg" / "mod.py").read_text(encoding="utf-8")

    assert record.status == SOURCE_STATUS_EXISTING
    assert record.selection_strategy == SELECTION_TARGET_EXACT
    assert HEAD_IMPORT_LINE in source
    assert HEAD_IMPORT_LINE not in (record.content or "")

    verdict = verify_replace_in_file(
        materialization,
        "pkg/mod.py",
        HEAD_IMPORT_LINE,
        synthetic_workspace,
        step_index=1,
        operation_index=1,
    )

    assert verdict.failure_code is None
    assert verdict.verified is True
    assert verdict.present_in_visible_span is False
    assert verdict.present_in_full_file_same_version is True
    assert verdict.visibility == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    assert verdict.recorded_version_identity == verdict.current_version_identity

    step = _replace_step("pkg/mod.py", HEAD_IMPORT_LINE, "from app.tu import utc_now\n")
    assert (
        PlannerService._step_has_stale_replace_ops(
            step,
            Path(synthetic_workspace),
            source_materialization=materialization,
        )
        is False
    )

    issues = _source_operation_contract_issues(
        [step],
        task_text=CALL_SITE_ONLY_TASK,
        project_dir=Path(synthetic_workspace),
        source_materialization=materialization,
    )
    assert issues["stale_replace_materialization"] == []
    assert issues["missing_source_materialization"] == []


# --- Case 2 — correct visible call-site replacement -----------------------


def test_case2_visible_call_site_replacement_remains_accepted(synthetic_workspace):
    materialization = _materialize(
        synthetic_workspace, CALL_SITE_ONLY_TASK, ["pkg/mod.py"]
    )
    record = materialization.file_map()["pkg/mod.py"]

    assert CALL_SITE_TEXT in (record.content or "")

    verdict = verify_replace_in_file(
        materialization, "pkg/mod.py", CALL_SITE_TEXT, synthetic_workspace
    )

    assert verdict.failure_code is None
    assert verdict.present_in_visible_span is True
    assert verdict.visibility == SOURCE_EVIDENCE_VISIBLE_IN_SPAN

    step = _replace_step("pkg/mod.py", CALL_SITE_TEXT, "utc_now().isoformat()")
    assert (
        PlannerService._step_has_stale_replace_ops(
            step,
            Path(synthetic_workspace),
            source_materialization=materialization,
        )
        is False
    )


# --- Case 3 — genuinely fabricated text -----------------------------------


def test_case3_fabricated_old_text_is_rejected(synthetic_workspace):
    materialization = _materialize(
        synthetic_workspace, CALL_SITE_ONLY_TASK, ["pkg/mod.py"]
    )
    fabricated = "def fabricated_helper():\n    return 'never written'\n"

    verdict = verify_replace_in_file(
        materialization, "pkg/mod.py", fabricated, synthetic_workspace
    )

    assert verdict.failure_code == FAILURE_STALE_OLD_TEXT
    assert verdict.verified is False
    assert verdict.present_in_visible_span is False
    assert verdict.present_in_full_file_same_version is False
    assert verdict.visibility == SOURCE_EVIDENCE_UNVERIFIED

    step = _replace_step("pkg/mod.py", fabricated, "pass\n")
    assert (
        PlannerService._step_has_stale_replace_ops(
            step,
            Path(synthetic_workspace),
            source_materialization=materialization,
        )
        is True
    )
    issues = _source_operation_contract_issues(
        [step],
        task_text=CALL_SITE_ONLY_TASK,
        project_dir=Path(synthetic_workspace),
        source_materialization=materialization,
    )
    assert issues["stale_replace_materialization"]


# --- Case 4 — wrong version -----------------------------------------------


def test_case4_version_change_after_materialization_is_rejected(synthetic_workspace):
    materialization = _materialize(
        synthetic_workspace, CALL_SITE_ONLY_TASK, ["pkg/mod.py"]
    )
    module = synthetic_workspace / "pkg" / "mod.py"
    module.write_text(
        module.read_text(encoding="utf-8") + "\n\n# appended after capture\n",
        encoding="utf-8",
    )

    verdict = verify_replace_in_file(
        materialization, "pkg/mod.py", HEAD_IMPORT_LINE, synthetic_workspace
    )

    assert HEAD_IMPORT_LINE in module.read_text(encoding="utf-8")
    assert verdict.failure_code == FAILURE_VERSION_CHANGED
    assert verdict.verified is False
    assert verdict.present_in_full_file_same_version is False
    assert verdict.visibility == SOURCE_EVIDENCE_UNVERIFIED

    step = _replace_step("pkg/mod.py", HEAD_IMPORT_LINE, "import os\n")
    assert (
        PlannerService._step_has_stale_replace_ops(
            step,
            Path(synthetic_workspace),
            source_materialization=materialization,
        )
        is True
    )


# --- Case 5 — second span contains the import region ----------------------


def _reference_workspace(tmp_path: Path) -> Path:
    repository_root = Path(__file__).resolve().parents[2]
    destination = tmp_path / REFERENCE_TARGET
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repository_root / REFERENCE_TARGET, destination)
    return tmp_path


def test_case5_expected_python_file_exposes_head_and_target_spans(tmp_path):
    workspace = _reference_workspace(tmp_path)
    materialization = _materialize(workspace, REFERENCE_TASK, REFERENCE_EXPECTED)
    record = materialization.file_map()[REFERENCE_TARGET]

    assert record.status == SOURCE_STATUS_EXISTING
    assert record.selection_strategy == SELECTION_TARGET_WITH_STRUCTURAL_HEAD
    assert [span.kind for span in record.spans] == [
        SPAN_STRUCTURAL_HEAD,
        SPAN_PRIMARY_TARGET,
    ]

    content = record.content or ""
    assert HEAD_IMPORT_LINE in content
    assert CALL_SITE_TEXT in content
    assert record.target_included is True
    assert record.included_source_bytes <= MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert len(content.encode("utf-8")) <= MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert materialization.materialized_source_bytes <= MAX_SOURCE_CONTENT_TOTAL_CHARS

    head, primary = record.spans
    assert head.start_byte == 0
    assert head.end_byte <= primary.start_byte
    assert head.start_line == 1
    assert primary.start_line > head.end_line

    prompt = materialization.to_prompt_block()
    assert f"{head.start_line}-{head.end_line}" in prompt
    assert f"{primary.start_line}-{primary.end_line}" in prompt
    assert SPAN_STRUCTURAL_HEAD in prompt


def test_case5_both_reference_replacements_are_model_visible(tmp_path):
    workspace = _reference_workspace(tmp_path)
    materialization = _materialize(workspace, REFERENCE_TASK, REFERENCE_EXPECTED)

    for old_text in (HEAD_IMPORT_LINE, CALL_SITE_TEXT):
        verdict = verify_replace_in_file(
            materialization, REFERENCE_TARGET, old_text, workspace
        )
        assert verdict.failure_code is None
        assert verdict.present_in_visible_span is True
        assert verdict.visibility == SOURCE_EVIDENCE_VISIBLE_IN_SPAN


# --- Case 6 — budget competition ------------------------------------------


def test_case6_expected_spans_are_funded_before_support_source(tmp_path):
    workspace = _reference_workspace(tmp_path)
    support = workspace / "app" / "support"
    support.mkdir(parents=True)
    for name in ("alpha.py", "beta.py", "gamma.py"):
        (support / name).write_text(
            "".join(
                f"def support_{name[0]}_{index}():\n    return {index}\n\n\n"
                for index in range(80)
            ),
            encoding="utf-8",
        )

    materialization = _materialize(
        workspace,
        REFERENCE_TASK,
        REFERENCE_EXPECTED,
        supporting=[
            "app/support/alpha.py",
            "app/support/beta.py",
            "app/support/gamma.py",
        ],
    )
    order = [item.relative_path for item in materialization.files]
    record = materialization.file_map()[REFERENCE_TARGET]

    assert order.index(REFERENCE_TARGET) < order.index("app/support/alpha.py")
    assert len(record.spans) == 2
    assert HEAD_IMPORT_LINE in (record.content or "")
    assert CALL_SITE_TEXT in (record.content or "")
    assert materialization.materialized_source_bytes <= MAX_SOURCE_CONTENT_TOTAL_CHARS
    assert all(
        item.content is None
        or len(item.content.encode("utf-8")) <= MAX_SOURCE_CONTENT_PER_FILE_CHARS
        for item in materialization.files
    )


# --- Case 7 — overlap ------------------------------------------------------


def test_case7_head_adjacent_target_keeps_exactly_one_span(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    early_target = (
        '"""Early target module."""\n'
        "\n"
        "import json\n"
        f"{HEAD_IMPORT_LINE}"
        "\n"
        "\n"
        "def exported_at() -> str:\n"
        f"    return {CALL_SITE_TEXT}\n"
        "\n"
        "\n"
    ) + "".join(
        f"def filler_{index}():\n    return json.dumps({index})\n\n\n"
        for index in range(120)
    )
    (package / "mod.py").write_text(early_target, encoding="utf-8")
    task = (
        "Replace the deprecated `datetime.utcnow()` call in pkg/mod.py and "
        "remove the now-unused datetime import from pkg/mod.py."
    )

    materialization = _materialize(tmp_path, task, ["pkg/mod.py"])
    record = materialization.file_map()["pkg/mod.py"]
    content = record.content or ""

    assert len(record.spans) == 1
    assert record.spans[0].kind == SPAN_PRIMARY_TARGET
    assert record.selection_strategy != SELECTION_TARGET_WITH_STRUCTURAL_HEAD
    assert content.count(HEAD_IMPORT_LINE) == 1
    assert content.count(CALL_SITE_TEXT) == 1
    assert len(content.encode("utf-8")) <= MAX_SOURCE_CONTENT_PER_FILE_CHARS


# --- Case 8 — UTF-8 safety -------------------------------------------------


def test_case8_multibyte_boundaries_stay_valid_utf8(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    multibyte = "é☃🌍" * 12
    # Multibyte text sits around both span boundaries.  Bytes near offset 4096
    # stay ASCII so the pre-existing fixed-sample binary sniffer is not the
    # subject of this case.
    head = (
        '"""Módulo sintético 🌍."""\n'
        "\n"
        "import json\n"
        f"{HEAD_IMPORT_LINE}"
        "from typing import Any\n"
        "\n"
    ) + "".join(
        f"HEAD_{index} = {multibyte!r}\n"
        f"def head_helper_{index}(value: Any) -> Any:\n"
        f"    return json.dumps({{'i': {index}, 'v': value}})\n"
        "\n"
        for index in range(4)
    )
    ascii_filler = "".join(
        f"def filler_{index}(value: Any) -> Any:\n"
        f'    """Plain ASCII filler {index}."""\n'
        f"    return json.dumps({{'index': {index}, 'value': value}})\n"
        "\n"
        "\n"
        for index in range(160)
    )
    tail = "".join(
        f"TAIL_{index} = {multibyte!r}\n"
        f"def tail_helper_{index}() -> str:\n"
        f"    return TAIL_{index}\n"
        "\n"
        for index in range(10)
    ) + ("def exported_at() -> str:\n" f"    return {CALL_SITE_TEXT}\n")
    text = head + ascii_filler + tail
    (package / "mod.py").write_text(text, encoding="utf-8")
    encoded_probe = text.encode("utf-8")
    assert encoded_probe[4000:4200].decode("utf-8").isascii()
    task = (
        "Replace the deprecated `datetime.utcnow()` call in pkg/mod.py and "
        "remove the unused datetime import from pkg/mod.py."
    )

    materialization = _materialize(tmp_path, task, ["pkg/mod.py"])
    record = materialization.file_map()["pkg/mod.py"]
    encoded = text.encode("utf-8")
    lines = text.splitlines(keepends=True)

    assert record.full_source_bytes == len(encoded)
    assert len(record.spans) == 2
    assert (record.content or "").encode("utf-8").decode("utf-8") == record.content
    for span in record.spans:
        body = encoded[span.start_byte : span.end_byte].decode("utf-8")
        assert body in (record.content or "")
        assert body == "".join(lines[span.start_line - 1 : span.end_line])
        assert span.included_source_bytes == span.end_byte - span.start_byte
    assert len((record.content or "").encode("utf-8")) <= (
        MAX_SOURCE_CONTENT_PER_FILE_CHARS
    )


# --- Case 9 — path safety --------------------------------------------------


def test_case9_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(
        f"{HEAD_IMPORT_LINE}SECRET = 1\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "escape.py").symlink_to(outside / "secret.py")

    materialization = _materialize(
        workspace,
        "Replace the `SECRET` declaration in pkg/escape.py.",
        ["pkg/escape.py"],
    )
    record = materialization.file_map()["pkg/escape.py"]

    assert record.status != SOURCE_STATUS_EXISTING
    assert record.content is None

    verdict = verify_replace_in_file(
        materialization, "pkg/escape.py", "SECRET = 1\n", workspace
    )
    assert verdict.verified is False
    assert verdict.failure_code == FAILURE_MISSING_MATERIALIZATION


# --- Case 10 — planner and validator parity -------------------------------


def test_case10_planner_and_validator_share_one_verdict(synthetic_workspace):
    materialization = _materialize(
        synthetic_workspace, CALL_SITE_ONLY_TASK, ["pkg/mod.py"]
    )
    plan = [
        _replace_step(
            "pkg/mod.py",
            HEAD_IMPORT_LINE,
            "from app.tu import utc_now\n",
            step_number=1,
        ),
        _replace_step(
            "pkg/mod.py",
            "def fabricated_helper():\n",
            "def other():\n",
            step_number=2,
        ),
    ]

    planner_issues = PlannerService.find_immediate_repair_step_issues(
        plan,
        Path(synthetic_workspace),
        source_materialization=materialization,
    )
    validator_issues = _source_operation_contract_issues(
        plan,
        task_text=CALL_SITE_ONLY_TASK,
        project_dir=Path(synthetic_workspace),
        source_materialization=materialization,
    )
    verdicts = validator_issues["source_operation_verdicts"]

    assert planner_issues.get("stale_replace_ops_steps") == [2]
    failing = [item for item in verdicts if item["failure_code"]]
    assert [item["step_index"] for item in failing] == [2]
    assert failing[0]["failure_code"] == FAILURE_STALE_OLD_TEXT

    accepted = [item for item in verdicts if not item["failure_code"]]
    assert accepted[0]["visibility"] == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION

    direct = verify_replace_in_file(
        materialization,
        "pkg/mod.py",
        "def fabricated_helper():\n",
        synthetic_workspace,
        step_index=2,
        operation_index=1,
    )
    assert direct.failure_code == failing[0]["failure_code"]
    assert direct.visibility == failing[0]["visibility"]


# --- Attempt 7 conventional-plan reconstruction ---------------------------


def _attempt7_plan(import_old: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Create the shared timezone-aware utc_now helper module",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/time_utils.py",
                    "content": (
                        '"""Shared timezone-aware UTC helpers."""\n\n'
                        "from datetime import datetime, timezone\n\n\n"
                        "def utc_now() -> datetime:\n"
                        '    """Return the current timezone-aware UTC datetime."""\n\n'
                        "    return datetime.now(timezone.utc)\n"
                    ),
                }
            ],
            "verification": "python3 -m py_compile app/time_utils.py",
            "rollback": "rm -f app/time_utils.py",
            "expected_files": ["app/time_utils.py"],
        },
        {
            "step_number": 2,
            "description": (
                "Import the shared helper in context_service.py and use it for "
                "the exported_at timestamp"
            ),
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": REFERENCE_TARGET,
                    "old": import_old,
                    "new": "from app.time_utils import utc_now\n",
                },
                {
                    "op": "replace_in_file",
                    "path": REFERENCE_TARGET,
                    "old": '"exported_at": datetime.utcnow().isoformat(),',
                    "new": '"exported_at": utc_now().isoformat(),',
                },
            ],
            "verification": f"python3 -m py_compile {REFERENCE_TARGET}",
            "rollback": None,
            "expected_files": [REFERENCE_TARGET],
        },
        {
            "step_number": 3,
            "description": "Add focused regression coverage for the helper",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/tests/test_utc_now_helper.py",
                    "content": (
                        "from datetime import timezone\n\n"
                        "from app.time_utils import utc_now\n\n\n"
                        "def test_utc_now_is_timezone_aware_utc():\n"
                        "    stamp = utc_now()\n"
                        "    assert stamp.tzinfo is not None\n"
                        "    assert stamp.utcoffset() == timezone.utc.utcoffset(None)\n"
                    ),
                }
            ],
            "verification": "python3 -m pytest app/tests/test_utc_now_helper.py -q",
            "rollback": "rm -f app/tests/test_utc_now_helper.py",
            "expected_files": ["app/tests/test_utc_now_helper.py"],
        },
    ]


def test_attempt7_conventional_plan_is_accepted_provider_free(tmp_path):
    workspace = _reference_workspace(tmp_path)
    source = (workspace / REFERENCE_TARGET).read_text(encoding="utf-8")
    # The import replacement is taken from the real current source.
    import_old = HEAD_IMPORT_LINE
    assert source.count(import_old) == 1

    materialization = _materialize(workspace, REFERENCE_TASK, REFERENCE_EXPECTED)
    plan = _attempt7_plan(import_old)

    for path in ("app/time_utils.py", "app/tests/test_utc_now_helper.py"):
        record = materialization.file_map()[path]
        assert record.status == SOURCE_STATUS_NEW
        assert record.creation_authorized is True

    target = materialization.file_map()[REFERENCE_TARGET]
    for old_text in (import_old, '"exported_at": datetime.utcnow().isoformat(),'):
        assert old_text in source
        assert old_text in (target.content or "")

    planner_issues = PlannerService.find_immediate_repair_step_issues(
        plan, Path(workspace), source_materialization=materialization
    )
    assert "stale_replace_ops_steps" not in planner_issues

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=REFERENCE_TASK,
        execution_profile="full_lifecycle",
        project_dir=workspace,
        title="Phase 32H-1 Attempt 7 conventional reconstruction",
        description=REFERENCE_TASK,
        source_materialization=materialization,
    )

    assert outcome.accepted, (
        outcome.rejection_reasons
        if hasattr(outcome, "rejection_reasons")
        else outcome.details
    )
