"""PR20-A1 — task-declared acceptance verification at completion.

Completion candidate validation chose its pytest scope only from
candidate-changed tests, Plan-declared tests and name-matched tests; the
task's own ``Acceptance criteria:`` command was ignored (PR19 Exit Review).
With ``TASK_ACCEPTANCE_VERIFICATION_ENABLED`` the admitted acceptance pytest
commands run in addition to the focused selection.  No provider is called.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.services.orchestration.validation.candidate_checks import (
    task_acceptance_pytest_commands,
    validate_candidate_delta,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    PLAN_ASSERTION,
    PREFIX_TEST,
    ROUTER_BASE,
    ROUTER_FIXED,
    _candidate_project,
)

# Tracked copy of PR17 evidence candidate-c.patch (docs/ is not in git); the
# digest is the one recorded in the PR17 evidence SHA256SUMS.
CANDIDATE_C_PATCH = (
    Path(__file__).resolve().parent / "fixtures/pr20_a1_candidate_c.patch"
)
CANDIDATE_C_PATCH_SHA256 = (
    "6ab6ffc62344c1b14c956b6d45c0783531cabdf17ae5d63a45fce79cc3680f2e"
)

# D2 C1 / Task 31 exact task text (sha256 0f56a25a…, PR17 benchmark-identity).
TASK31_PROMPT = (
    "After the 2.0.0 refactor (see CHANGELOG.md), downstream aggregation in "
    "dataflow/aggregate.py double counts customers. Version 1.4.0 behavior was: "
    "records whose ids differ only by case or surrounding whitespace are the same "
    "customer, and the most recently updated version wins.\n\n"
    "Current failing test output:\n"
    "$ python -m pytest -q\n"
    "FAILED tests/test_aggregate.py::test_case_and_whitespace_variants_count_once"
    " - assert 5 == 3\n"
    "FAILED tests/test_aggregate.py::test_region_totals_use_latest_version_of_each"
    "_customer - AssertionError: assert {'eu': 1099, 'us': 250} == "
    "{'eu': 100, 'us': 150}\n"
    "2 failed, 3 passed\n\n"
    "Diagnose the regression from this evidence and fix the actual defect in the "
    "implementation. Do not change test expectations.\n\n"
    "Acceptance criteria:\n"
    "1. `python -m pytest -q` passes (all tests in tests/).\n"
    "2. Do not modify anything under tests/.\n"
    "3. Change only files under dataflow/.\n"
    "4. Existing passing tests (tests/test_normalize.py) must keep passing."
)
TASK31_PROMPT_SHA256 = (
    "0f56a25acef9ca68ccc932b5441bda6affed041638a92cde7947e8c783840992"
)

# Retained Task 31 seed sources (hashes pinned in PR17 benchmark-identity.json).
TASK31_SEED = {
    "dataflow/__init__.py": "",
    "dataflow/aggregate.py": (
        '"""Downstream aggregation over normalized records."""\n\n'
        "from collections import defaultdict\n"
        "from typing import Dict, Iterable\n\n"
        "from dataflow.normalize import normalize_records\n\n\n"
        "def totals_by_region(records: Iterable[dict]) -> Dict[str, int]:\n"
        "    totals: Dict[str, int] = defaultdict(int)\n"
        "    for record in normalize_records(records):\n"
        '        totals[record["region"]] += record["balance"]\n'
        "    return dict(totals)\n\n\n"
        "def customer_count(records: Iterable[dict]) -> int:\n"
        "    return len(normalize_records(records))\n"
    ),
    "dataflow/normalize.py": (
        '"""Record normalization.\n\n'
        "Upstream feeds send the same customer record several times, with ids that\n"
        "differ only in case or surrounding whitespace. ``normalize_records`` returns\n"
        "one record per logical id, keeping the most recently updated version.\n"
        '"""\n\n'
        "from typing import Dict, Iterable, List\n\n\n"
        "def normalize_id(raw_id: str) -> str:\n"
        "    return raw_id.strip().lower()\n\n\n"
        "def normalize_records(records: Iterable[dict]) -> List[dict]:\n"
        "    latest: Dict[str, dict] = {}\n"
        "    for record in records:\n"
        '        key = record["id"]\n'
        "        current = latest.get(key)\n"
        '        if current is None or record["updated_at"] >= current["updated_at"]:\n'
        '            latest[key] = {**record, "id": normalize_id(record["id"])}\n'
        '    return sorted(latest.values(), key=lambda item: item["id"])\n'
    ),
    "tests/__init__.py": "",
    "tests/test_aggregate.py": (
        '"""Regression tests for the duplicate-id handling broken by the 2.0 '
        'refactor."""\n\n'
        "from dataflow.aggregate import customer_count, totals_by_region\n\n"
        "FEED = [\n"
        '    {"id": "C-001", "updated_at": 10, "region": "us", "balance": 100},\n'
        '    {"id": " c-001", "updated_at": 20, "region": "us", "balance": 150},\n'
        '    {"id": "C-002", "updated_at": 5, "region": "eu", "balance": 40},\n'
        '    {"id": "c-002 ", "updated_at": 1, "region": "eu", "balance": 999},\n'
        '    {"id": "C-003", "updated_at": 7, "region": "eu", "balance": 60},\n'
        "]\n\n\n"
        "def test_case_and_whitespace_variants_count_once():\n"
        "    assert customer_count(FEED) == 3\n\n\n"
        "def test_region_totals_use_latest_version_of_each_customer():\n"
        '    assert totals_by_region(FEED) == {"us": 150, "eu": 100}\n'
    ),
    "tests/test_normalize.py": (
        "from dataflow.normalize import normalize_id, normalize_records\n\n\n"
        "def test_normalize_id_strips_and_lowercases():\n"
        '    assert normalize_id("  AbC ") == "abc"\n\n\n'
        "def test_distinct_ids_are_all_kept():\n"
        "    records = [\n"
        '        {"id": "b", "updated_at": 1, "region": "eu", "balance": 1},\n'
        '        {"id": "a", "updated_at": 1, "region": "us", "balance": 2},\n'
        "    ]\n"
        '    assert [r["id"] for r in normalize_records(records)] == ["a", "b"]\n\n\n'
        "def test_exact_duplicate_keeps_latest():\n"
        "    records = [\n"
        '        {"id": "a", "updated_at": 1, "region": "us", "balance": 5},\n'
        '        {"id": "a", "updated_at": 3, "region": "us", "balance": 7},\n'
        "    ]\n"
        "    assert normalize_records(records) == [\n"
        '        {"id": "a", "updated_at": 3, "region": "us", "balance": 7}\n'
        "    ]\n"
    ),
}
TASK31_SEED_SHA256 = {
    "dataflow/__init__.py": (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ),
    "dataflow/aggregate.py": (
        "4f64a50121ed54336e4c4ce420b59299a8e4d6b626e79788114e6f102803fb67"
    ),
    "dataflow/normalize.py": (
        "d43da52484e71e67e9134fd0b251464cd06fa7695dee0a2d00fed7f5b8e64eb3"
    ),
    "tests/__init__.py": (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ),
    "tests/test_aggregate.py": (
        "047034f908d90500b002c3a6c66c3cd843922570e0211c89bdc7c24d804d5a9a"
    ),
    "tests/test_normalize.py": (
        "e0ec049babfa9932c32a96c535dceed22d5c0653f27b3cad91416b364f1d2331"
    ),
}
CANDIDATE_C_NORMALIZE_SHA256 = (
    "5832ffc6cd4bb1e7eeb89d2975f1b7457ec708334a31174821a7e69c11445e8d"
)
AGGREGATE_ONLY_PLAN = [
    {
        "step_number": 1,
        "description": "Fix duplicate handling",
        "verification": "python -m pytest tests/test_aggregate.py -q",
    }
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task31_project(tmp_path: Path) -> Path:
    root = tmp_path / "task31"
    for path, text in TASK31_SEED.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return root


def _apply_candidate_c(root: Path) -> None:
    removed, added = [], []
    patch = CANDIDATE_C_PATCH.read_text(encoding="utf-8")
    assert _sha256(patch) == CANDIDATE_C_PATCH_SHA256
    for line in patch.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    target = root / "dataflow/normalize.py"
    text = target.read_text(encoding="utf-8")
    old, new = "\n".join(removed) + "\n", "\n".join(added) + "\n"
    assert text.count(old) == 1
    target.write_text(text.replace(old, new), encoding="utf-8")


def _apply_incomplete_candidate(root: Path) -> None:
    # Fixes the dedup key but drops the sorted() contract: aggregation is
    # order-insensitive (aggregate tests pass) while test_normalize breaks.
    target = root / "dataflow/normalize.py"
    text = target.read_text(encoding="utf-8")
    text = text.replace('key = record["id"]', 'key = normalize_id(record["id"])')
    text = text.replace(
        'return sorted(latest.values(), key=lambda item: item["id"])',
        "return list(latest.values())",
    )
    target.write_text(text, encoding="utf-8")


def _run(root, changed, plan=(), prompt=TASK31_PROMPT, *, enabled=True):
    return validate_candidate_delta(
        project_dir=root,
        change_set={"modified_files": list(changed)},
        plan=list(plan),
        task_prompt=prompt,
        task_acceptance_enabled=enabled,
    )


def _rule_ids(run):
    return [finding.rule_id for finding in run.findings]


def _admitted(text, project_dir=None):
    extraction = task_acceptance_pytest_commands(text, project_dir=project_dir)
    return [command.text for command in extraction.admitted]


def _refusals(text, project_dir=None):
    extraction = task_acceptance_pytest_commands(text, project_dir=project_dir)
    return [refusal["reason"] for refusal in extraction.refused]


def _section(*commands: str) -> str:
    return "Fix it.\n\nAcceptance criteria:\n" + "".join(
        f"- `{command}` passes.\n" for command in commands
    )


# --- fixture fidelity -------------------------------------------------------


def test_task31_fixtures_match_retained_pr17_identity():
    assert _sha256(TASK31_PROMPT) == TASK31_PROMPT_SHA256
    for path, text in TASK31_SEED.items():
        assert _sha256(text) == TASK31_SEED_SHA256[path], path


# --- extraction -------------------------------------------------------------


def test_task31_acceptance_text_extracts_only_the_acceptance_command():
    extraction = task_acceptance_pytest_commands(TASK31_PROMPT)

    # The "$ python -m pytest -q" in the failing-output block is not extracted.
    assert extraction.section_found is True
    assert extraction.extracted == ("python -m pytest -q",)
    assert [c.text for c in extraction.admitted] == ["python -m pytest -q"]
    assert extraction.admitted[0].pytest_args == ("-q",)
    assert extraction.refused == ()


@pytest.mark.parametrize(
    "command, args",
    [
        (
            "python -m pytest tests/test_normalize.py -q",
            ("tests/test_normalize.py", "-q"),
        ),
        ("pytest tests/test_aggregate.py -q", ("tests/test_aggregate.py", "-q")),
        ("python3 -m pytest tests/test_a.py::test_b", ("tests/test_a.py::test_b",)),
        ("pytest", ()),
    ],
)
def test_explicit_pytest_path_commands_are_admitted(command, args):
    extraction = task_acceptance_pytest_commands(_section(command))

    assert [(c.text, c.pytest_args) for c in extraction.admitted] == [(command, args)]


def test_no_acceptance_section_produces_no_commands():
    extraction = task_acceptance_pytest_commands(
        "Fix the bug.\nRun `python -m pytest -q` when done."
    )

    assert extraction.section_found is False
    assert extraction.extracted == extraction.admitted == extraction.refused == ()


def test_ordinary_prose_is_not_executable():
    extraction = task_acceptance_pytest_commands(
        "Acceptance criteria:\n"
        "1. All tests pass when running python -m pytest -q.\n"
        "2. `customer_count` returns 3 and `tests/` is unchanged.\n"
        "3. `make test` succeeds.\n"
    )

    assert extraction.section_found is True
    assert extraction.extracted == extraction.admitted == extraction.refused == ()
    assert [item["text"] for item in extraction.ignored] == [
        "customer_count",
        "tests/",
        "make test",
    ]
    assert {item["reason"] for item in extraction.ignored} == {"not_pytest_command"}


def test_section_ends_at_next_heading():
    prompt = (
        "Acceptance criteria:\n- `pytest tests/test_a.py`\n\n"
        "Notes:\n- `pytest tests/test_b.py` is slow; skip it.\n"
    )

    assert _admitted(prompt) == ["pytest tests/test_a.py"]


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q && rm -rf build",
        "pytest -q; echo done",
        "pytest -q | tee out.txt",
        "pytest -q > out.txt",
        "pytest $(cat tests.txt)",
        "pytest $HOME/tests",
        "pytest tests/*.py",
        'python -c "import pytest"',
        "pytest ~/tests",
    ],
)
def test_unsafe_shell_syntax_is_refused(command):
    assert _admitted(_section(command)) == []
    assert _refusals(_section(command)) == ["shell_metacharacter"]


@pytest.mark.parametrize(
    "command",
    [
        "PYTHONPATH=. pytest -q",
        "/usr/bin/python -m pytest -q",
        "venv/bin/pytest -q",
        "uv run pytest -q",
        "python -X dev -m pytest -q",
        "cd tests && pytest",
    ],
)
def test_unsupported_executable_forms_are_refused(command):
    extraction = task_acceptance_pytest_commands(_section(command))

    assert extraction.admitted == ()
    assert len(extraction.refused) == 1
    assert extraction.refused[0]["reason"] in {
        "unsupported_executable",
        "shell_metacharacter",
    }


@pytest.mark.parametrize(
    "command",
    [
        "pytest -p myplugin",
        "pytest -c other.ini",
        "pytest --rootdir=/",
        "pytest --confcutdir=..",
        "pytest -o addopts=-pfoo",
        "pytest --pyargs dataflow",
        "pytest -k normalize",
        "pytest --import-mode=importlib",
        "pytest --basetemp=/tmp/x",
    ],
)
def test_unsupported_pytest_options_are_refused(command):
    assert _refusals(_section(command)) == ["unsupported_pytest_option"]


def test_workspace_traversal_and_absolute_paths_are_refused(tmp_path):
    root = _task31_project(tmp_path)

    assert _refusals(_section("pytest ../other/tests"), root) == ["path_traversal"]
    assert _refusals(_section("pytest tests/../../x"), root) == ["path_traversal"]
    assert _refusals(_section("pytest /etc"), root) == ["absolute_path"]
    assert _refusals(_section("pytest /dev/null"), root) == ["absolute_path"]


def test_symlink_escape_is_refused(tmp_path):
    root = _task31_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_x.py").write_text("def test_x():\n    pass\n")
    os.symlink(outside, root / "tests" / "linked")

    prompt = _section("pytest tests/linked/test_x.py")
    assert _refusals(prompt, root) == ["path_escapes_workspace"]
    # Without a workspace only lexical checks apply.
    assert _admitted(prompt) == ["pytest tests/linked/test_x.py"]


def test_refusals_are_recorded_and_never_executed(tmp_path):
    root = _task31_project(tmp_path)
    _apply_candidate_c(root)
    prompt = _section("python -m pytest -q", "pytest -q && rm -rf /")

    run = _run(root, ["dataflow/normalize.py"], prompt=prompt)

    details = run.task_acceptance
    assert details["admitted"] == ["python -m pytest -q"]
    assert details["refused"] == [
        {"command": "pytest -q && rm -rf /", "reason": "shell_metacharacter"}
    ]
    assert [r["command"] for r in details["results"]] == ["python -m pytest -q"]
    assert details["results"][0]["outcome"] == "passed"
    assert not any("rm -rf" in command for command in run.commands_run)
    # A refused explicit acceptance command blocks certification.
    assert _rule_ids(run) == ["task_acceptance_command_refused"]
    refused = run.findings[0]
    assert (refused.severity, refused.repairable, refused.source) == (
        "error",
        False,
        "task_acceptance",
    )


# --- REENTRY-3 --------------------------------------------------------------


def _reentry3_project(tmp_path):
    root = _candidate_project(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/router.py": ROUTER_BASE,
            "tests/test_routes.py": PREFIX_TEST,
        },
    )
    (root / "app/router.py").write_text(ROUTER_FIXED, encoding="utf-8")
    assert subprocess.run(PLAN_ASSERTION, shell=True, cwd=root).returncode == 0
    return root


REENTRY3_PROMPT = (
    "Repair the permissions route\n\nAcceptance criteria:\n"
    "- `python -m pytest -q` passes.\n"
)


def test_reentry3_acceptance_command_detects_failing_repository_test(tmp_path):
    root = _reentry3_project(tmp_path)

    run = _run(
        root,
        ["app/router.py"],
        [{"verification": PLAN_ASSERTION}],
        prompt=REENTRY3_PROMPT,
    )

    # Existing focused selection still selects nothing and reports nothing.
    assert run.selection.source == "no_trustworthy_focused_tests"
    assert _rule_ids(run) == ["task_acceptance_pytest_failed"]
    finding = run.findings[0]
    assert finding.severity == "error"
    assert finding.source == "task_acceptance"
    assert finding.category == "test"
    assert finding.attribution == "unknown"
    assert finding.repairable is False
    assert "test_permissions_prefix_registered" in finding.evidence["output"]
    assert run.task_acceptance["results"][0]["outcome"] == "failed"


def test_reentry3_without_flag_is_unchanged_gr7_pin(tmp_path):
    root = _reentry3_project(tmp_path)

    run = _run(
        root,
        ["app/router.py"],
        [{"verification": PLAN_ASSERTION}],
        prompt=REENTRY3_PROMPT,
        enabled=False,
    )

    assert run.selection.source == "no_trustworthy_focused_tests"
    assert _rule_ids(run) == []
    assert run.task_acceptance is None


# --- Task 31 ----------------------------------------------------------------


def test_task31_incomplete_candidate_is_caught_only_by_task_acceptance(tmp_path):
    root = _task31_project(tmp_path)
    _apply_incomplete_candidate(root)

    run = _run(root, ["dataflow/normalize.py"], AGGREGATE_ONLY_PLAN)

    assert run.selection.source == "plan_declared_focused_tests"
    assert run.selection.paths == ("tests/test_aggregate.py",)
    assert "focused_pytest_failed" not in _rule_ids(run)
    assert _rule_ids(run) == ["task_acceptance_pytest_failed"]
    assert "test_distinct_ids_are_all_kept" in run.findings[0].evidence["output"]
    result = run.task_acceptance["results"][0]
    assert (result["command"], result["executed"], result["outcome"]) == (
        "python -m pytest -q",
        True,
        "failed",
    )

    unflagged = _run(
        root, ["dataflow/normalize.py"], AGGREGATE_ONLY_PLAN, enabled=False
    )
    assert _rule_ids(unflagged) == []


def test_task31_candidate_c_passes_acceptance_tests(tmp_path):
    root = _task31_project(tmp_path)
    _apply_candidate_c(root)
    assert (
        hashlib.sha256((root / "dataflow/normalize.py").read_bytes()).hexdigest()
        == CANDIDATE_C_NORMALIZE_SHA256
    )

    run = _run(root, ["dataflow/normalize.py"], AGGREGATE_ONLY_PLAN)

    assert _rule_ids(run) == []
    assert run.task_acceptance["results"] == [
        {
            "command": "python -m pytest -q",
            "executed_command": run.commands_run[1],
            "executed": True,
            "returncode": 0,
            "outcome": "passed",
        }
    ]


def test_identical_focused_invocation_is_not_executed_twice(tmp_path):
    root = _task31_project(tmp_path)
    _apply_incomplete_candidate(root)
    prompt = _section("python -m pytest tests/test_aggregate.py")

    run = _run(root, ["dataflow/normalize.py"], AGGREGATE_ONLY_PLAN, prompt=prompt)

    assert run.commands_run.count(run.selection.command) == 1
    result = run.task_acceptance["results"][0]
    assert result["covered_by"] == "focused_test_selection"
    assert result["outcome"] == "passed"
    assert _rule_ids(run) == []


# --- compatibility ----------------------------------------------------------


def test_flag_defaults_off():
    assert (
        Settings.model_fields["TASK_ACCEPTANCE_VERIFICATION_ENABLED"].default is False
    )


def test_flag_off_preserves_existing_results(tmp_path):
    root = _task31_project(tmp_path)
    _apply_candidate_c(root)

    with_section = _run(
        root, ["dataflow/normalize.py"], AGGREGATE_ONLY_PLAN, enabled=False
    )
    plain = _run(
        root,
        ["dataflow/normalize.py"],
        AGGREGATE_ONLY_PLAN,
        prompt="Fix the regression",
        enabled=False,
    )

    assert with_section == plain
    assert with_section.task_acceptance is None


def test_no_acceptance_section_preserves_existing_behavior(tmp_path):
    root = _task31_project(tmp_path)
    _apply_incomplete_candidate(root)

    enabled = _run(
        root,
        ["dataflow/normalize.py"],
        AGGREGATE_ONLY_PLAN,
        prompt="Fix the regression",
    )
    disabled = _run(
        root,
        ["dataflow/normalize.py"],
        AGGREGATE_ONLY_PLAN,
        prompt="Fix the regression",
        enabled=False,
    )

    assert enabled.findings == disabled.findings
    assert enabled.commands_run == disabled.commands_run
    assert enabled.task_acceptance["section_found"] is False
    assert enabled.task_acceptance["results"] == []


def _complete(root, *, flag):
    evidence = {
        "candidate_delta_required": True,
        "run_candidate_checks": True,
        "include_static_checks": False,
        "summary_generated": True,
        "execution_results_count": 1,
        "reported_changed_files": ["dataflow/normalize.py"],
        "change_set": {
            "added_files": [],
            "modified_files": ["dataflow/normalize.py"],
            "deleted_files": [],
        },
    }
    if flag:
        evidence["run_task_acceptance_checks"] = True
    return ValidatorService.validate_task_completion(
        project_dir=root,
        plan=[
            {
                **AGGREGATE_ONLY_PLAN[0],
                "expected_files": ["dataflow/normalize.py"],
            }
        ],
        task_prompt=TASK31_PROMPT,
        execution_profile="full_lifecycle",
        completion_evidence=evidence,
    )


def test_validator_surfaces_acceptance_details_and_rejects(tmp_path):
    root = _task31_project(tmp_path)
    _apply_incomplete_candidate(root)

    off = _complete(root, flag=False)
    on = _complete(root, flag=True)

    assert "task_acceptance_verification" not in off.details
    assert "task_acceptance_pytest_failed" not in [f.rule_id for f in off.findings]
    assert off.status != "rejected"

    assert on.status == "rejected"
    assert "task_acceptance_pytest_failed" in [f.rule_id for f in on.findings]
    assert on.details["focused_test_selection"] == off.details["focused_test_selection"]
    acceptance = on.details["task_acceptance_verification"]
    assert acceptance["admitted"] == ["python -m pytest -q"]
    assert acceptance["results"][0]["outcome"] == "failed"
    assert [f["rule_id"] for f in on.details["test_findings"]] == [
        "task_acceptance_pytest_failed"
    ]
