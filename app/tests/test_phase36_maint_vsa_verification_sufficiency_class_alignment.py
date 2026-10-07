"""PHASE36-MAINT-VSA — verification-sufficiency class alignment.

The Phase 36 closure evaluation showed two gaps in
``ValidatorService.validate_task_completion``:

* VSA-1: GR8's repair class was lexical (8 keywords).  OAD restorative wording
  ("Restore ...", "Reinstate ...", "Re-enable ...") is the same repair risk but
  kept no GR8 protection, so smoke-only evidence plus an evaluator PASS
  auto-promoted.  The GR8 class now also accepts OAD's existing restorative
  mutation-intent regex.
* VSA-2: a mutation-capable task whose Candidate changes Product source, with
  ``missing`` or ``insufficient`` applicable verification, now sets
  ``verification_insufficient`` (warning; held for review, not rejected).

Feature/refactor smoke-only stays governed by Phase 10L (unchanged).  Verdicts
come from the real validator and run through the real
``CompletionCoordinator.complete_task``; only the evaluator is scripted.  No
provider is called.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.models import LogEntry
from app.services.orchestration.phases.execution_local_steps import (
    execute_verification_command,
)
from app.services.orchestration.phases.completion_flow import (
    _build_evaluator_verification_evidence,
)
from app.services.orchestration.validation.integrity import (
    classify_verification_command,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    SUBSTANTIVE_PASS,
    _Runtime,
    _seed_auto_publish,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORRECT_SRC = "def status():\n    return 'ready'\n"
INCORRECT_SRC = "def status():\n    return 'brokn'\n"
BEHAVIORAL = "python -c \"from app import status; assert status() == 'ready'\""
SMOKE = 'python -c "import app"'
INSUFFICIENT = "cat app.py"
MISSING = ""
HOLD_REASON = "verification_insufficient_for_auto_promotion"
# REENTRY-14 shape: PASS whose NOTES claim verification that did not run.
UNSUPPORTED_PASS = (
    "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3\n"
    "TOTAL: 10/10\n"
    "VERDICT: PASS\n"
    "NOTES: all endpoints were verified at their intended locations."
)

FIX = "Fix the bug in the status function."
RESTORE = "Restore the status function to return ready."
FEATURE = "Add a status function that returns ready."
REFACTOR = "Refactor the status module without changing behavior."


def _verdict(
    tmp_path,
    *,
    text,
    verification,
    src=CORRECT_SRC,
    files=None,
    changed=("app.py",),
    change_set=None,
    name="verdict-project",
):
    project_dir = tmp_path / name
    project_dir.mkdir(exist_ok=True)
    for rel, content in (files or {"app.py": src}).items():
        target = project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    evidence = {
        "summary_generated": True,
        "execution_results_count": 1,
        "reported_changed_files": list(changed),
    }
    if change_set is not None:
        evidence["change_set"] = change_set
    return ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Update status",
                "verification": verification,
                "expected_files": list(changed) or ["app.py"],
            }
        ],
        task_prompt=text,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=text,
        completion_evidence=evidence,
    )


def _ve(verdict):
    return verdict.details["validation_evidence"]


def _held_reason(db_session, ctx):
    entries = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(
            LogEntry.message.like(
                "[ORCHESTRATION] Held task workspace for manual review%"
            )
        )
        .all()
    )
    if not entries:
        return None, None
    payload = json.loads(entries[-1].log_metadata)
    return payload.get("reason"), payload.get("review_decision")


def _publish(db_session, tmp_path, monkeypatch, verdict, evaluator=SUBSTANTIVE_PASS):
    """Return (auto_promoted, hold_reason, publication_eligible, evaluator_calls)."""

    runtime = _Runtime({"output": evaluator})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, verdict
    )
    assert result["status"] == "completed"
    readme = project_root / "README.md"
    published = readme.exists() and readme.read_text(encoding="utf-8") == "after\n"
    reason, decision = _held_reason(db_session, ctx)
    eligible = (
        bool(published) if decision is None else decision.get("publication_eligible")
    )
    return published, reason, eligible, runtime.evaluator_calls


def _assert_auto_promoted(outcome):
    published, reason, eligible, calls = outcome
    assert published is True and reason is None and eligible is True
    assert calls == 1


def _assert_held_insufficient(outcome):
    published, reason, eligible, _calls = outcome
    assert published is False
    assert reason == HOLD_REASON
    assert eligible is False


# --- VSA-1 classification ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Restore the intended behavior.",
        "Reinstate the intended behavior.",
        "Re-enable the intended behavior.",
        "Reenable the worker registration.",
        "Prevent the duplicated behavior.",
        "Make the endpoint work again.",
    ],
)
def test_oad_restorative_wording_is_in_gr8_repair_class(text):
    assert ValidatorService.repair_requires_independent_evidence(text) is True


@pytest.mark.parametrize(
    "text",
    [
        FEATURE,
        REFACTOR,
        "Add a new endpoint.",
        "Update the route.",
        "Verify the restore script.",
        "Make sure the endpoint works again.",
        "Document the status function.",
    ],
)
def test_non_restorative_wording_stays_outside_gr8_repair_class(text):
    assert ValidatorService.repair_requires_independent_evidence(text) is False


def test_restorative_class_does_not_change_explicit_repair_intent():
    # ``has_explicit_repair_intent`` gates the fresh-bootstrap exemption and is
    # deliberately unchanged.
    assert ValidatorService.has_explicit_repair_intent(RESTORE) is False
    assert ValidatorService.has_explicit_repair_intent(FIX) is True


# --- V1-V12 ----------------------------------------------------------------


def test_v1_fix_behavioral_pass_auto_promotes(db_session, tmp_path, monkeypatch):
    verdict = _verdict(tmp_path, text=FIX, verification=BEHAVIORAL)
    assert verdict.status == "accepted"
    assert _ve(verdict)["verification_insufficient"] is False
    _assert_auto_promoted(_publish(db_session, tmp_path, monkeypatch, verdict))


def test_v2_restore_behavioral_pass_matches_v1(db_session, tmp_path, monkeypatch):
    verdict = _verdict(tmp_path, text=RESTORE, verification=BEHAVIORAL)
    assert verdict.status == "accepted"
    assert _ve(verdict)["requires_independent_evidence"] is True
    assert _ve(verdict)["verification_insufficient"] is False
    _assert_auto_promoted(_publish(db_session, tmp_path, monkeypatch, verdict))


@pytest.mark.parametrize("text", [FIX, RESTORE], ids=["v3_fix", "v4_restore"])
def test_v3_v4_repair_class_smoke_only_pass_is_held(
    db_session, tmp_path, monkeypatch, text
):
    verdict = _verdict(tmp_path, text=text, verification=SMOKE)
    assert verdict.status == "warning" and verdict.accepted
    assert _ve(verdict)["command_quality"] == "smoke_only"
    assert _ve(verdict)["verification_insufficient"] is True
    _assert_held_insufficient(_publish(db_session, tmp_path, monkeypatch, verdict))


def test_v5_restore_incorrect_candidate_smoke_hallucinated_pass_is_held(
    db_session, tmp_path, monkeypatch
):
    verdict = _verdict(tmp_path, text=RESTORE, verification=SMOKE, src=INCORRECT_SRC)
    assert _ve(verdict)["verification_insufficient"] is True
    _assert_held_insufficient(
        _publish(db_session, tmp_path, monkeypatch, verdict, UNSUPPORTED_PASS)
    )


@pytest.mark.parametrize("text", [FEATURE, REFACTOR], ids=["v6_feature", "v7_refactor"])
def test_v6_v7_non_repair_smoke_only_policy_is_unchanged(
    db_session, tmp_path, monkeypatch, text
):
    # Phase 10L documented limitation: VSA does not hold non-repair smoke-only.
    verdict = _verdict(tmp_path, text=text, verification=SMOKE, src=INCORRECT_SRC)
    assert verdict.status == "accepted"
    assert _ve(verdict)["requires_independent_evidence"] is False
    assert _ve(verdict)["candidate_source_mutation"] is True
    assert _ve(verdict)["verification_insufficient"] is False
    _assert_auto_promoted(
        _publish(db_session, tmp_path, monkeypatch, verdict, UNSUPPORTED_PASS)
    )


@pytest.mark.parametrize(
    "verification", [INSUFFICIENT, MISSING], ids=["v8_insufficient", "v9_missing"]
)
def test_v8_v9_feature_source_mutation_without_verification_is_held(
    db_session, tmp_path, monkeypatch, verification
):
    verdict = _verdict(tmp_path, text=FEATURE, verification=verification)
    evidence = _ve(verdict)
    # VSA-2 is a hold, not a rejection: the Candidate is kept for review.
    assert verdict.status == "warning" and verdict.accepted
    assert evidence["requires_independent_evidence"] is False
    assert evidence["candidate_source_mutation"] is True
    assert evidence["source_mutation_paths"] == ["app.py"]
    assert evidence["verification_insufficient"] is True
    _assert_held_insufficient(_publish(db_session, tmp_path, monkeypatch, verdict))


@pytest.mark.parametrize(
    "verification", [INSUFFICIENT, MISSING], ids=["v10_insufficient", "v11_missing"]
)
def test_v10_v11_restore_without_verification_is_rejected_like_fix(
    tmp_path, verification
):
    restore = _verdict(tmp_path, text=RESTORE, verification=verification)
    fix = _verdict(tmp_path, text=FIX, verification=verification, name="fix")
    for verdict in (restore, fix):
        # GR8 rejection is preserved (stronger than a hold) and now covers
        # restorative wording too.
        assert verdict.status == "rejected" and not verdict.accepted
        assert _ve(verdict)["verification_insufficient"] is True
        assert any(
            "Repair task verification is insufficient" in reason
            for reason in verdict.reasons
        )


def test_v12_failed_behavioral_verification_is_terminal_before_review(tmp_path):
    project_dir = tmp_path / "v12"
    project_dir.mkdir()
    (project_dir / "app.py").write_text(INCORRECT_SRC, encoding="utf-8")
    result = execute_verification_command(
        project_dir=project_dir, command=BEHAVIORAL, timeout_seconds=30
    )
    assert result.get("success") is False
    assert classify_verification_command(BEHAVIORAL) == "behavioral"


# --- Semantic-equivalence matrix -------------------------------------------


@pytest.mark.parametrize(
    ("explicit", "restorative"),
    [
        ("Fix the duplicated route.", "Restore the intended route."),
        (
            "Repair the endpoint behavior.",
            "Reinstate the intended endpoint behavior.",
        ),
        ("Fix the worker registration.", "Re-enable the worker registration."),
    ],
)
def test_semantic_repair_wording_has_equivalent_publication_authority(
    db_session, tmp_path, monkeypatch, explicit, restorative
):
    outcomes = []
    for index, text in enumerate((explicit, restorative)):
        verdict = _verdict(
            tmp_path, text=text, verification=SMOKE, name=f"pair-{index}"
        )
        assert verdict.status == "warning"
        assert _ve(verdict)["verification_insufficient"] is True
        outcomes.append(verdict.status)
    assert outcomes[0] == outcomes[1]
    _assert_held_insufficient(
        _publish(
            db_session,
            tmp_path,
            monkeypatch,
            _verdict(tmp_path, text=restorative, verification=SMOKE, name="pub"),
        )
    )


# --- Source-mutation predicate controls --------------------------------------


@pytest.mark.parametrize(
    ("label", "files", "changed", "expected_source"),
    [
        ("no_changed_files", {"app.py": CORRECT_SRC}, (), False),
        (
            "test_only",
            {"app.py": CORRECT_SRC, "tests/test_app.py": "def test_x():\n    pass\n"},
            ("tests/test_app.py",),
            False,
        ),
        ("docs_only", {"README.md": "# Docs\n"}, ("README.md",), False),
        ("config_only", {"settings.json": "{}\n"}, ("settings.json",), False),
        (
            "orchestration_internal",
            {".agent/notes.py": "x = 1\n"},
            (".agent/notes.py",),
            False,
        ),
        ("source", {"app.py": CORRECT_SRC}, ("app.py",), True),
    ],
)
def test_source_mutation_floor_controls(
    tmp_path, label, files, changed, expected_source
):
    verdict = _verdict(
        tmp_path,
        text="Update the status module.",
        verification=MISSING,
        files=files,
        changed=changed,
    )
    evidence = _ve(verdict)
    assert evidence["candidate_source_mutation"] is expected_source
    assert evidence["verification_insufficient"] is expected_source


def test_source_mutation_uses_change_set_paths_when_present(tmp_path):
    # The Change Set is authoritative over the reported file list.
    docs_only = _verdict(
        tmp_path,
        text=FEATURE,
        verification=MISSING,
        changed=("app.py",),
        change_set={"added_files": [], "modified_files": ["README.md"]},
    )
    assert _ve(docs_only)["candidate_source_mutation"] is False
    assert _ve(docs_only)["verification_insufficient"] is False
    source = _verdict(
        tmp_path,
        text=FEATURE,
        verification=MISSING,
        changed=(),
        change_set={"added_files": [], "modified_files": ["app.py"]},
        name="cs-source",
    )
    assert _ve(source)["candidate_source_mutation"] is True
    assert _ve(source)["verification_insufficient"] is True


def test_verification_profile_is_not_mutation_capable(tmp_path):
    verdict = _verdict(tmp_path, text="Verify the route.", verification=MISSING)
    assert verdict.profile == "verification"
    assert _ve(verdict)["candidate_source_mutation"] is False
    assert _ve(verdict)["verification_insufficient"] is False


def test_scaffold_profile_follows_the_existing_source_classifier(tmp_path):
    text = "Scaffold the project skeleton."
    assert (
        ValidatorService.infer_validation_profile(text, "full_lifecycle", title=text)
        == "scaffold"
    )
    manifest_only = _verdict(
        tmp_path,
        text=text,
        verification=MISSING,
        files={"package.json": "{}\n"},
        changed=("package.json",),
    )
    assert _ve(manifest_only)["candidate_source_mutation"] is False
    assert _ve(manifest_only)["verification_insufficient"] is False
    # A scaffold that writes source files is a source mutation by the same
    # suffix rule as ``pre_existing_source_files``; it is held, not rejected.
    with_source = _verdict(
        tmp_path,
        text=text,
        verification=MISSING,
        files={"pkg/__init__.py": ""},
        changed=("pkg/__init__.py",),
        name="scaffold-source",
    )
    assert _ve(with_source)["candidate_source_mutation"] is True
    assert with_source.accepted
    assert _ve(with_source)["verification_insufficient"] is True


def test_behavioral_feature_source_mutation_is_unchanged(tmp_path):
    verdict = _verdict(tmp_path, text=FEATURE, verification=BEHAVIORAL)
    assert verdict.status == "accepted"
    assert _ve(verdict)["candidate_source_mutation"] is True
    assert _ve(verdict)["verification_insufficient"] is False


# --- Threat model -----------------------------------------------------------


def test_t5_evaluator_cannot_upgrade_smoke_only(db_session, tmp_path, monkeypatch):
    verdict = _verdict(tmp_path, text=RESTORE, verification=SMOKE)
    projected = _build_evaluator_verification_evidence(None, verdict)
    assert projected["quality"]["command_quality"] == "smoke_only"
    assert projected["quality"]["verification_insufficient"] is True
    _assert_held_insufficient(
        _publish(db_session, tmp_path, monkeypatch, verdict, UNSUPPORTED_PASS)
    )
    assert _ve(verdict)["command_quality"] == "smoke_only"


def test_t8_existing_fix_rejection_is_not_weakened(tmp_path):
    for verification in (MISSING, INSUFFICIENT):
        verdict = _verdict(
            tmp_path, text=FIX, verification=verification, name=f"t8{verification!r}"
        )
        assert verdict.status == "rejected"


# --- REENTRY-14 counterfactual -----------------------------------------------

R14_TITLE = "Restore the intended permissions API endpoint paths"
R14_DESCRIPTION = (
    "The permissions API routes are exposed under duplicated /permissions "
    "segments. Restore the intended permission endpoint paths so each endpoint "
    "is available under a single /api/v1/permissions/... prefix, preserve the "
    "existing permission behavior, and verify the correction."
)
R14_BASELINE_COMMIT = "e7a952b8aded8ed9a78de419fbb355bdacf07414"
R14_CANDIDATE_SHA256 = (
    "e1b8cc525b725930ae8df797c835cfed21de6544e2abbcd284b9d557f7d8aad5"
)
R14_PLAN = [
    {
        "step_number": 1,
        "description": "Inspect permissions router",
        "verification": (
            "grep -n 'prefix\\|APIRouter' app/api/v1/endpoints/permissions.py"
        ),
        "expected_files": [],
    },
    {
        "step_number": 2,
        "description": "Remove duplicated prefix",
        "verification": "grep -A 3 'permissions.router' app/api/v1/router.py",
        "expected_files": ["app/api/v1/router.py"],
        "ops": [
            {
                "op": "replace_in_file",
                "path": "app/api/v1/router.py",
                "old": '    permissions.router,\n    prefix="/permissions",\n',
                "new": "    permissions.router,\n",
            }
        ],
    },
    {
        "step_number": 3,
        "description": "Import check",
        "verification": (
            'python -c "import app.api.v1.router; '
            "print('Router imported successfully')\""
        ),
        "expected_files": [],
    },
]


def _r14_candidate_router():
    try:
        baseline = subprocess.run(
            ["git", "show", f"{R14_BASELINE_COMMIT}:app/api/v1/router.py"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("REENTRY-14 baseline commit is unavailable")
    candidate = baseline.replace(
        '    permissions.router,\n    prefix="/permissions",\n',
        "    permissions.router,\n",
        1,
    )
    import hashlib

    assert hashlib.sha256(candidate.encode()).hexdigest() == R14_CANDIDATE_SHA256
    return candidate


def test_reentry14_counterfactual_is_held_under_vsa(db_session, tmp_path, monkeypatch):
    project_dir = tmp_path / "r14"
    target = project_dir / "app/api/v1/router.py"
    target.parent.mkdir(parents=True)
    target.write_text(_r14_candidate_router(), encoding="utf-8")
    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=R14_PLAN,
        task_prompt=R14_DESCRIPTION,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=R14_TITLE,
        description=R14_DESCRIPTION,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 3,
            "reported_changed_files": ["app/api/v1/router.py"],
            "step_changed_files": [
                {"step_number": 1, "files_changed": []},
                {"step_number": 2, "files_changed": ["app/api/v1/router.py"]},
                {"step_number": 3, "files_changed": []},
            ],
            "change_set": {
                "added_files": [],
                "modified_files": ["app/api/v1/router.py"],
                "deleted_files": [],
            },
        },
    )
    evidence = _ve(verdict)
    assert verdict.profile == "implementation"
    assert evidence["command_quality"] == "smoke_only"
    assert evidence["applicable_command_quality"] == "smoke_only"
    assert evidence["requires_independent_evidence"] is True
    assert evidence["verification_insufficient"] is True
    assert verdict.status == "warning" and verdict.accepted
    _assert_held_insufficient(
        _publish(db_session, tmp_path, monkeypatch, verdict, UNSUPPORTED_PASS)
    )
