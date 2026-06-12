from __future__ import annotations

import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.orchestration.phases.planning_repair_arbitration_control import (
    _repair_drops_bootstrap_obligations,
    arbitrate_planning_repair_candidate,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)


def _bootstrap_plan(package: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Create package skeleton",
            "commands": [],
            "verification": (
                f"test -f {package}/__init__.py && "
                "test -f setup.py && test -f requirements.txt"
            ),
            "rollback": None,
            "expected_files": [
                f"{package}/__init__.py",
                "tests/__init__.py",
                "setup.py",
                "requirements.txt",
            ],
            "ops": [
                {"op": "mkdir", "path": package},
                {
                    "op": "write_file",
                    "path": f"{package}/__init__.py",
                    "content": "__version__ = '0.1.0'\n",
                },
                {"op": "mkdir", "path": "tests"},
                {
                    "op": "write_file",
                    "path": "tests/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "setup.py",
                    "content": (
                        "from setuptools import setup\n"
                        f"setup(name='{package}', version='0.1.0', "
                        f"packages=['{package}'])\n"
                    ),
                },
                {
                    "op": "write_file",
                    "path": "requirements.txt",
                    "content": "pytest\n",
                },
            ],
        },
        {
            "step_number": 2,
            "description": "Create virtual environment and install dependencies",
            "commands": [
                "python3 -m venv .venv",
                ".venv/bin/pip install -e .",
                ".venv/bin/pip install -r requirements.txt",
            ],
            "verification": f".venv/bin/pip show {package}",
            "rollback": "rm -rf .venv",
            "expected_files": [],
            "ops": [],
        },
        {
            "step_number": 3,
            "description": "Verify test discovery",
            "commands": [".venv/bin/python3 -m pytest --collect-only"],
            "verification": ".venv/bin/python3 -m pytest --collect-only",
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]


def _collapsed_pathtools_repair() -> list[dict]:
    files = [
        "pathtools/__init__.py",
        "tests/__init__.py",
        "setup.py",
    ]
    verify = (
        'python -c "import pathlib,sys; files='
        + repr(files)
        + '; sys.exit(0 if all(pathlib.Path(p).exists() for p in files) else 1)"'
    )
    return [
        {
            "step_number": 1,
            "description": "Inspect workspace",
            "commands": ["rg --files . | sort"],
            "verification": 'python -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
        {
            "step_number": 2,
            "description": "Create package skeleton",
            "commands": [verify],
            "verification": verify,
            "rollback": None,
            "expected_files": files,
            "ops": [
                {"op": "mkdir", "path": "pathtools"},
                {
                    "op": "write_file",
                    "path": "pathtools/__init__.py",
                    "content": "__version__ = '0.1.0'\n",
                },
                {"op": "mkdir", "path": "tests"},
                {
                    "op": "write_file",
                    "path": "tests/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "setup.py",
                    "content": (
                        "from setuptools import setup\n"
                        "setup(name='pathtools', version='0.1.0', "
                        "packages=['pathtools'])\n"
                    ),
                },
            ],
        },
        {
            "step_number": 3,
            "description": "Verify requested change",
            "commands": [verify],
            "verification": verify,
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]


def _collapsed_strtools_repair() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Create package structure",
            "commands": [],
            "verification": (
                "python -c \"import sys; sys.path.insert(0, '.'); "
                "import strtools; assert strtools.__version__ == '0.1.0'\""
            ),
            "rollback": None,
            "expected_files": [
                "strtools/__init__.py",
                "tests/__init__.py",
                "setup.py",
            ],
            "ops": [
                {"op": "mkdir", "path": "strtools"},
                {"op": "mkdir", "path": "tests"},
                {
                    "op": "write_file",
                    "path": "strtools/__init__.py",
                    "content": "__version__ = '0.1.0'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "setup.py",
                    "content": (
                        "from setuptools import setup\n"
                        "setup(name='strtools', version='0.1.0', "
                        "packages=['strtools'])\n"
                    ),
                },
            ],
        },
        {
            "step_number": 2,
            "description": "Install package",
            "commands": ["pip install -e ."],
            "verification": (
                'python -c "import strtools; print(strtools.__version__)"'
            ),
            "rollback": "pip uninstall -y strtools",
            "expected_files": [],
            "ops": [],
        },
        {
            "step_number": 3,
            "description": "Verify package",
            "commands": ["python -m pytest --collect-only"],
            "verification": (
                'python -c "import strtools; '
                "assert strtools.__version__ == '0.1.0'\""
            ),
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]


def _ctx(
    *,
    plan: list[dict],
    project_dir: Path,
    package: str,
    plan_position: int = 1,
    prompt: str | None = None,
) -> SimpleNamespace:
    prompt = prompt or f"Bootstrap the {package} package with a venv and pytest."
    task = SimpleNamespace(
        title=(
            f"Bootstrap {package} package"
            if plan_position == 1
            else f"Implement {package} feature"
        ),
        description=prompt,
        plan_position=plan_position,
        status=None,
        error_message=None,
    )
    return SimpleNamespace(
        task=task,
        orchestration_state=SimpleNamespace(
            plan=plan,
            project_dir=project_dir,
            project_context="",
            status=None,
            abort_reason=None,
            reasoning_artifact=None,
        ),
        prompt=prompt,
        execution_profile="full_lifecycle",
        validation_severity="standard",
        workflow_profile=None,
        workflow_stage=None,
        session_id=1,
        task_id=1,
        task_execution_id=1,
        session_instance_id=None,
        logger=logging.getLogger("test.weak_verification_repair_acceptance"),
        emit_live=MagicMock(),
        db=MagicMock(),
        restore_workspace_snapshot_if_needed=None,
    )


def _pathtools_t2_plan(*, weak_verification: bool = True) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Implement path filters",
            "commands": [],
            "verification": ("python3 -m py_compile pathtools/filters.py"),
            "rollback": None,
            "expected_files": ["pathtools/filters.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "pathtools/filters.py",
                    "content": (
                        "def filter_by_extension(paths, ext):\n"
                        "    return [p for p in paths if p.endswith(ext)]\n\n"
                        "def filter_by_prefix(paths, prefix):\n"
                        "    return [p for p in paths if p.startswith(prefix)]\n"
                    ),
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Create filter tests",
            "commands": [],
            "verification": (".venv/bin/python3 -m pytest tests/test_filters.py -q"),
            "rollback": None,
            "expected_files": ["tests/test_filters.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_filters.py",
                    "content": (
                        "from pathtools.filters import filter_by_extension\n\n"
                        "def test_filter_by_extension():\n"
                        "    assert filter_by_extension(['a.py'], '.py') == ['a.py']\n"
                    ),
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Verify filters",
            "commands": [".venv/bin/python3 -m pytest tests/test_filters.py -q"],
            "verification": (
                "test -f tests/test_filters.py"
                if weak_verification
                else ".venv/bin/python3 -m pytest tests/test_filters.py -q"
            ),
            "rollback": None,
            "expected_files": ["pathtools/filters.py", "tests/test_filters.py"],
            "ops": [],
        },
    ]


def _placeholder_pathtools_t2_repair() -> list[dict]:
    candidate = _pathtools_t2_plan(weak_verification=False)
    candidate[1]["commands"] = ["touch tests/test_filters.py"]
    candidate[2]["commands"] = [
        "touch pathtools/filters.py",
        "touch tests/test_filters.py",
    ]
    return candidate


def test_regressed_weak_verification_repair_preserves_complete_bootstrap_plan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _bootstrap_plan("pathtools")
    candidate = _collapsed_pathtools_repair()
    ctx = _ctx(plan=candidate, project_dir=tmp_path, package="pathtools")
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "replace"
    assert ctx.orchestration_state.plan[1:] == original[1:]
    assert ctx.orchestration_state.plan[0]["ops"] == original[0]["ops"]
    assert (
        ctx.orchestration_state.plan[0]["verification"] == candidate[1]["verification"]
    )


def test_strtools_regression_preserves_venv_install_and_test_obligations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _bootstrap_plan("strtools")
    candidate = _collapsed_strtools_repair()
    ctx = _ctx(plan=candidate, project_dir=tmp_path, package="strtools")
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "replace"
    assert ctx.orchestration_state.plan[1:] == original[1:]
    assert (
        ctx.orchestration_state.plan[0]["verification"] == candidate[0]["verification"]
    )


def test_full_shape_weak_verification_improvement_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _bootstrap_plan("pathtools")
    candidate = copy.deepcopy(original)
    candidate[0]["verification"] = (
        'python -c "import pathlib; '
        "assert pathlib.Path('requirements.txt').read_text().strip() == 'pytest'\""
    )
    ctx = _ctx(plan=candidate, project_dir=tmp_path, package="pathtools")
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "none"
    assert ctx.orchestration_state.plan == candidate


def test_non_weak_repair_acceptance_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _bootstrap_plan("pathtools")
    original[0]["verification"] = (
        'python -c "import pathlib; '
        "assert pathlib.Path('requirements.txt').exists()\""
    )
    candidate = _collapsed_pathtools_repair()
    ctx = _ctx(plan=candidate, project_dir=tmp_path, package="pathtools")
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "none"
    assert ctx.orchestration_state.plan == candidate


def test_non_bootstrap_placeholder_repair_preserves_original_implementation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _pathtools_t2_plan()
    candidate = _placeholder_pathtools_t2_repair()
    original_issues = {
        key: value
        for key, value in PlannerService.find_immediate_repair_step_issues(
            original,
            project_dir=tmp_path,
        ).items()
        if value
    }
    immediate_issues = PlannerService.find_immediate_repair_step_issues(
        candidate,
        project_dir=tmp_path,
    )
    assert original_issues == {"weak_verification_steps": [3]}
    assert immediate_issues["placeholder_only_steps"] == [2, 3]
    ctx = _ctx(
        plan=candidate,
        project_dir=tmp_path,
        package="pathtools",
        plan_position=2,
        prompt=(
            "Create pathtools/filters.py and tests/test_filters.py, then run "
            ".venv/bin/python3 -m pytest tests/test_filters.py -q."
        ),
    )
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues=immediate_issues,
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "replace"
    assert ctx.orchestration_state.plan[:2] == original[:2]
    assert (
        ctx.orchestration_state.plan[2]["verification"] == candidate[2]["verification"]
    )
    assert ctx.orchestration_state.plan[2]["commands"] == original[2]["commands"]


def test_non_bootstrap_real_test_creation_with_test_rewrite_is_accepted(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _pathtools_t2_plan()
    candidate = _pathtools_t2_plan(weak_verification=False)
    candidate[1]["ops"][0]["content"] += (
        "\ndef test_filter_by_prefix():\n"
        "    from pathtools.filters import filter_by_prefix\n"
        "    assert filter_by_prefix(['alpha'], 'a') == ['alpha']\n"
    )
    ctx = _ctx(
        plan=candidate,
        project_dir=tmp_path,
        package="pathtools",
        plan_position=2,
    )
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
    classification = classify_planning_repair_candidate(
        previous_plan=original,
        repaired_plan=candidate,
        project_dir=tmp_path,
        immediate_repair_issues={},
    )
    assert "test_rewrite" not in classification["regression_labels"]
    assert classification["outcome"] == "improved_or_preserved"

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "none"
    assert ctx.orchestration_state.plan == candidate


def test_preservation_ignores_non_blocking_original_advisories(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    original = _bootstrap_plan("pathtools")
    original[0]["commands"] = [
        "python -c \"from pathlib import Path; Path('pathtools/__init__.py').write_text('')\""
    ]
    original[0].pop("ops", None)
    candidate = _collapsed_pathtools_repair()
    ctx = _ctx(plan=candidate, project_dir=tmp_path, package="pathtools")
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=copy.deepcopy(original),
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=lambda **kwargs: {"output": "[]"},
    )

    assert result["action"] == "replace"


def test_bootstrap_obligation_path_compare_normalizes_dot_slash():
    previous = [
        {
            "expected_files": ["./src/app.py", "tests/test_app.py"],
            "commands": ["python -m venv .venv", "pip install -e .", "pytest -q"],
        }
    ]
    candidate = [
        {
            "expected_files": ["src/app.py", "./tests/test_app.py"],
            "commands": ["python -m venv .venv", "pip install -e .", "pytest -q"],
        }
    ]

    assert _repair_drops_bootstrap_obligations(previous, candidate) is False
