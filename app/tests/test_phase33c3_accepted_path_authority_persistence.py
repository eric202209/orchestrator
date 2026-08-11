"""Phase 33C-3 — Accepted Path Authority construction and persistence.

The authority is minted only after deterministic Plan acceptance, persisted in
the existing accepted plan verdict, and reconstructable from the existing
``TaskCheckpoint`` evidence.  Nothing consumes it yet: this phase is write-side
only and sits before the 33C-4 behavioural rollback boundary.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.models import Project, Session as SessionModel, Task
from app.services.orchestration.coordinators.completion_coordinator import (
    _completion_plan_identity,
)
from app.services.orchestration.planning.source_materialization import (
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_MISSING,
    SOURCE_STATUS_NEW,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.state.persistence import record_validation_verdict
from app.services.orchestration.types import (
    PlanAccepted,
    PlanRejected,
    PlanRepairRequired,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
    accepted_plan_identity,
    build_accepted_path_authority,
    maximum_scope_digest,
    plan_identity_text,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrantError,
)
from app.services.orchestration.validation.validator import ValidatorService

VERIFY = "python3 -m pytest -q"

PRODUCTION_ROOT = Path(__file__).resolve().parents[1] / "services"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _validate(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
    task_prompt: str,
    title: str = "Phase 33C-3",
    execution_profile: str = "implementation",
    validation_severity: str = "standard",
    source_materialization: Any = None,
):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task_prompt,
        execution_profile=execution_profile,
        project_dir=project_dir,
        title=title,
        validation_severity=validation_severity,
        source_materialization=source_materialization,
    )


def _existing_record(
    relative_path: str,
    *,
    workspace_identity: str,
    content_hash: str,
) -> MaterializedSourceFile:
    return MaterializedSourceFile(
        relative_path=relative_path,
        workspace_identity=workspace_identity,
        content="materialized",
        content_hash=content_hash,
        version_identity="dev:ino:size:mtime",
        status=SOURCE_STATUS_EXISTING,
        truncated=False,
        source_length=12,
        source_length_chars=12,
        included_prompt_length=12,
        expected=True,
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_only_plan() -> list[dict[str, Any]]:
    return [
        {
            "step_number": 1,
            "description": "Inspect the workspace and report the current test outcome",
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [],
        }
    ]


def _authority_of(outcome) -> dict[str, Any]:
    authority = outcome.details.get("accepted_path_authority")
    assert authority is not None, outcome.reasons
    return authority


def _grants_by_path(authority: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {grant["path"]: grant for grant in authority["grants"]}


# ---------------------------------------------------------------------------
# grant construction through the real accepted-plan path
# ---------------------------------------------------------------------------


def test_existing_mutable_grant_from_accepted_existing_file_rewrite(tmp_path):
    (tmp_path / "app").mkdir()
    target = tmp_path / "app" / "money.py"
    target.write_text("def fmt(c):\n    return str(c)\n", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": (
                "Rewrite app/money.py with the corrected formatter (full file replace)"
            ),
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "git checkout -- app/money.py",
            "expected_files": ["app/money.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/money.py",
                    "content": 'def fmt(c):\n    return f"${c / 100:.2f}"\n',
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt=(
            "Rewrite the existing formatter in app/money.py so the existing tests "
            "pass. Replace the full file content. Verify with python3 -m pytest -q."
        ),
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons

    authority = _authority_of(outcome)
    grant = _grants_by_path(authority)["app/money.py"]
    assert grant["grant_class"] == GrantClass.EXISTING_MUTABLE.value
    assert grant["provenance"] == GrantProvenance.ACCEPTED_PLAN.value

    materialized = {
        item["relative_path"]: item
        for item in outcome.details["source_materialization"]["files"]
    }
    # The baseline hash is the source-grounding hash already measured for the
    # plan, not a second filesystem read performed for the authority.
    assert (
        grant["baseline_content_hash"] == materialized["app/money.py"]["content_hash"]
    )
    assert grant["baseline_content_hash"] is not None


def test_existing_readonly_grant_for_context_file_without_requested_mutation(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Create app/feature.py exposing feature() from app/helpers.py",
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "rm -f app/feature.py",
            "expected_files": ["app/feature.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/feature.py",
                    "content": (
                        "from app.helpers import VALUE\n\n\n"
                        "def feature() -> int:\n    return VALUE\n"
                    ),
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt=(
            "Create a new module app/feature.py that reads VALUE from "
            "app/helpers.py. Verify with python3 -m pytest -q."
        ),
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons

    grants = _grants_by_path(_authority_of(outcome))
    readonly = grants["app/helpers.py"]
    assert readonly["grant_class"] == GrantClass.EXISTING_READONLY.value
    assert readonly["provenance"] == GrantProvenance.SOURCE_GROUNDING.value
    assert readonly["baseline_content_hash"] is not None


def test_creation_authorized_grant_carries_no_baseline_hash(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Create app/feature.py exposing feature() from app/helpers.py",
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "rm -f app/feature.py",
            "expected_files": ["app/feature.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/feature.py",
                    "content": (
                        "from app.helpers import VALUE\n\n\n"
                        "def feature() -> int:\n    return VALUE\n"
                    ),
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt=(
            "Create a new module app/feature.py that reads VALUE from "
            "app/helpers.py. Verify with python3 -m pytest -q."
        ),
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons

    created = _grants_by_path(_authority_of(outcome))["app/feature.py"]
    assert created["grant_class"] == GrantClass.CREATION_AUTHORIZED.value
    assert created["provenance"] == GrantProvenance.ACCEPTED_PLAN.value
    assert created["baseline_content_hash"] is None


def test_task_explicit_scope_is_the_immediate_provenance_when_present(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "money.py").write_text(
        "def fmt(c):\n    return str(c)\n", encoding="utf-8"
    )

    plan = [
        {
            "step_number": 1,
            "description": (
                "Rewrite app/money.py with the corrected formatter (full file replace)"
            ),
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "git checkout -- app/money.py",
            "expected_files": ["app/money.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/money.py",
                    "content": 'def fmt(c):\n    return f"${c / 100:.2f}"\n',
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt=(
            "Change only app/money.py: rewrite the existing formatter and replace "
            "the full file content. Verify with python3 -m pytest -q."
        ),
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons

    grant = _grants_by_path(_authority_of(outcome))["app/money.py"]
    assert grant["provenance"] == GrantProvenance.TASK_EXPLICIT_SCOPE.value
    assert grant["grant_class"] == GrantClass.EXISTING_MUTABLE.value


def test_unauthorized_creation_is_not_accepted_and_mints_no_authority(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")

    # The plan writes a path that source grounding never admitted for creation:
    # it is not an expected file of the step, so existing plan validation rejects
    # the creation before any authority question arises.
    plan = [
        {
            "step_number": 1,
            "description": "Update helper wiring",
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": ["app/helpers.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/invented.py",
                    "content": "INVENTED = True\n",
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Adjust the helper wiring. Verify with python3 -m pytest -q.",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    "app/helpers.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("helpers"),
                ),
                MaterializedSourceFile(
                    relative_path="app/invented.py",
                    workspace_identity=str(tmp_path),
                    content=None,
                    content_hash=None,
                    version_identity=None,
                    status=SOURCE_STATUS_MISSING,
                    truncated=False,
                    source_length=None,
                    source_length_chars=None,
                    included_prompt_length=0,
                    expected=False,
                    creation_authorized=False,
                ),
            ),
        ),
    )
    assert not isinstance(outcome, PlanAccepted)
    assert "accepted_path_authority" not in outcome.details


def test_rejected_plan_never_carries_an_authority(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "",
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Do something unspecified.",
    )
    assert isinstance(outcome, (PlanRejected, PlanRepairRequired))
    assert "accepted_path_authority" not in outcome.details


def test_high_severity_warning_plan_is_rejected_and_mints_no_authority(tmp_path):
    """A plan that cannot enter Execution must not mint authority.

    Under ``severity="high"`` the policy escalates ``warning`` to ``rejected``,
    so the authority must be gated on the *final* status, not on the raw finding
    lists.
    """

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": (
                "Confirm the new feature module is present before running the suite"
            ),
            "commands": ["test -f app/feature.py", VERIFY],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create app/feature.py exposing feature() from app/helpers.py",
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "rm -f app/feature.py",
            "expected_files": ["app/feature.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/feature.py",
                    "content": (
                        "from app.helpers import VALUE\n\n\n"
                        "def feature() -> int:\n    return VALUE\n"
                    ),
                }
            ],
        },
    ]
    task_prompt = (
        "Create a new module app/feature.py that reads VALUE from app/helpers.py. "
        "Verify with python3 -m pytest -q."
    )

    warned = _validate(plan, project_dir=tmp_path, task_prompt=task_prompt)
    assert isinstance(warned, PlanAccepted)
    assert warned.status == "warning"
    # A warning plan still enters Execution, so it is authoritative and mints.
    assert "accepted_path_authority" in warned.details

    escalated = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt=task_prompt,
        validation_severity="high",
    )
    assert isinstance(escalated, PlanRejected)
    assert escalated.status == "rejected"
    assert "accepted_path_authority" not in escalated.details


def test_no_deletion_grants_are_constructed(tmp_path):
    """Deletion authority is not derivable from accepted-plan facts today."""

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "stale.py").write_text("STALE = 1\n", encoding="utf-8")

    authority, _ = build_accepted_path_authority(
        plan=[
            {
                "step_number": 1,
                "description": "Delete the stale module",
                "ops": [{"op": "delete_file", "path": "app/stale.py"}],
            }
        ],
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    "app/stale.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("stale"),
                ),
            ),
        ),
    )
    classes = {grant.grant_class for grant in authority.grants}
    assert GrantClass.DELETION_AUTHORIZED not in classes
    # A requested deletion does not even confer mutation authority.
    assert classes == {GrantClass.EXISTING_READONLY}


# ---------------------------------------------------------------------------
# fail-closed domain rejections
# ---------------------------------------------------------------------------


def test_case_aliased_source_facts_fail_closed_with_no_authority(tmp_path):
    (tmp_path / "App").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "App" / "Real.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "app" / "real.py").write_text("B = 2\n", encoding="utf-8")

    outcome = _validate(
        _read_only_plan(),
        project_dir=tmp_path,
        task_prompt="Review the repository and report test status.",
        execution_profile="review_only",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    "App/Real.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("a"),
                ),
                _existing_record(
                    "app/real.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("b"),
                ),
            ),
        ),
    )
    assert isinstance(outcome, PlanRejected)
    assert "accepted_path_authority" not in outcome.details
    assert outcome.details["accepted_path_authority_error"]["code"] == (
        "path_alias_conflict"
    )
    assert any(
        reason.startswith("accepted_path_authority_construction_failed")
        for reason in outcome.reasons
    )


def test_nested_grant_conflict_fails_closed(tmp_path):
    outcome = _validate(
        _read_only_plan(),
        project_dir=tmp_path,
        task_prompt="Review the repository and report test status.",
        execution_profile="review_only",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    "app",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("a"),
                ),
                _existing_record(
                    "app/real.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("b"),
                ),
            ),
        ),
    )
    assert isinstance(outcome, PlanRejected)
    assert "accepted_path_authority" not in outcome.details
    assert outcome.details["accepted_path_authority_error"]["code"] == "path_conflict"


def test_undeclarable_source_path_is_denied_by_absence_not_by_laundering(tmp_path):
    """An undeclarable path receives no grant; absence of a grant is denial."""

    authority, undeclarable = build_accepted_path_authority(
        plan=[
            {
                "step_number": 1,
                "description": "Touch a protected control surface",
                "ops": [{"op": "write_file", "path": ".git/config", "content": "x"}],
            }
        ],
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    ".git/config",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("git"),
                ),
                _existing_record(
                    "app/real.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("real"),
                ),
            ),
        ),
    )
    assert [grant.path.value for grant in authority.grants] == ["app/real.py"]
    assert undeclarable == (".git/config:path_protected_root",)


# ---------------------------------------------------------------------------
# identity binding
# ---------------------------------------------------------------------------


def _authority_for(
    *,
    workspace_identity: str = "/workspace/alpha",
    plan: Any = None,
    hashes: tuple[str, ...] = ("alpha", "beta"),
    reverse: bool = False,
) -> AcceptedPathAuthority:
    records = [
        _existing_record(
            "app/one.py",
            workspace_identity=workspace_identity,
            content_hash=_digest(hashes[0]),
        ),
        _existing_record(
            "app/two.py",
            workspace_identity=workspace_identity,
            content_hash=_digest(hashes[1]),
        ),
    ]
    if reverse:
        records.reverse()
    authority, _ = build_accepted_path_authority(
        plan=plan if plan is not None else [{"step_number": 1, "ops": []}],
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=workspace_identity,
            files=tuple(records),
        ),
    )
    return authority


def test_identity_is_independent_of_input_ordering():
    assert (
        _authority_for().authority_identity
        == _authority_for(reverse=True).authority_identity
    )


def test_identity_changes_when_a_source_hash_changes():
    assert (
        _authority_for().authority_identity
        != _authority_for(hashes=("alpha", "beta-moved")).authority_identity
    )


def test_identity_changes_when_the_accepted_plan_changes():
    assert (
        _authority_for(plan=[{"step_number": 1, "ops": []}]).authority_identity
        != _authority_for(
            plan=[{"step_number": 1, "ops": [], "description": "changed"}]
        ).authority_identity
    )


def test_identity_changes_when_the_workspace_identity_changes():
    assert (
        _authority_for(workspace_identity="/workspace/alpha").authority_identity
        != _authority_for(workspace_identity="/workspace/beta").authority_identity
    )


def test_maximum_scope_digest_is_deterministic_and_excluded_from_identity(tmp_path):
    materialization = PlannerSourceMaterialization(
        workspace_identity=str(tmp_path),
        files=(
            _existing_record(
                "app/one.py",
                workspace_identity=str(tmp_path),
                content_hash=_digest("one"),
            ),
        ),
    )
    unscoped = maximum_scope_digest(
        task_explicit_scope_paths=(),
        source_materialization=materialization,
    )
    scoped = maximum_scope_digest(
        task_explicit_scope_paths={"app/one.py"},
        source_materialization=materialization,
    )
    assert unscoped != scoped
    assert maximum_scope_digest(
        task_explicit_scope_paths={"b/y.py", "a/x.py"},
        source_materialization=materialization,
    ) == maximum_scope_digest(
        task_explicit_scope_paths=["a/x.py", "b/y.py"],
        source_materialization=materialization,
    )

    authority, _ = build_accepted_path_authority(
        plan=[{"step_number": 1, "ops": []}],
        source_materialization=materialization,
    )
    identity_only = AcceptedPathAuthority.compute_identity(
        grants=authority.grants,
        accepted_plan_identity=authority.accepted_plan_identity,
        workspace_identity=authority.workspace_identity,
    )
    assert identity_only == authority.authority_identity


def test_plan_identity_has_exactly_one_canonical_authority():
    plan = [{"step_number": 1, "description": "one", "ops": []}]
    assert _completion_plan_identity(plan) == plan_identity_text(plan)
    assert (
        accepted_plan_identity(plan)
        == hashlib.sha256(plan_identity_text(plan).encode("utf-8")).hexdigest()
    )


def test_non_serializable_plan_fails_closed_rather_than_crashing():
    with pytest.raises(PathGrantError) as excinfo:
        accepted_plan_identity([{"step_number": 1, "ops": {Path("/tmp")}}])
    assert excinfo.value.code == "accepted_plan_identity_invalid"


# ---------------------------------------------------------------------------
# persistence and restart reconstruction
# ---------------------------------------------------------------------------


def _seed_task(db) -> tuple[int, int]:
    project = Project(name="Phase 33C-3", workspace_path="/tmp/phase33c3")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 33C-3 session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db.add(session)
    db.flush()
    task = Task(
        project_id=project.id,
        title="Rewrite the formatter",
        description="Phase 33C-3 persistence proof",
        status="running",
    )
    db.add(task)
    db.flush()
    return int(session.id), int(task.id)


def test_authority_survives_a_fresh_session_reload_from_task_checkpoint(
    db_session, db_session_factory, tmp_path
):
    from app.models import TaskCheckpoint

    project_dir = tmp_path / "workspace"
    (project_dir / "app").mkdir(parents=True)
    (project_dir / "app" / "money.py").write_text(
        "def fmt(c):\n    return str(c)\n", encoding="utf-8"
    )

    plan = [
        {
            "step_number": 1,
            "description": (
                "Rewrite app/money.py with the corrected formatter (full file replace)"
            ),
            "commands": [VERIFY],
            "verification": VERIFY,
            "rollback": "git checkout -- app/money.py",
            "expected_files": ["app/money.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/money.py",
                    "content": 'def fmt(c):\n    return f"${c / 100:.2f}"\n',
                }
            ],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=project_dir,
        task_prompt=(
            "Rewrite the existing formatter in app/money.py so the existing tests "
            "pass. Replace the full file content. Verify with python3 -m pytest -q."
        ),
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons
    in_memory = AcceptedPathAuthority.from_dict(_authority_of(outcome))

    session_id, task_id = _seed_task(db_session)
    state = OrchestrationState(
        session_id=str(session_id),
        task_description="Rewrite the formatter",
        project_name="Phase 33C-3",
        task_id=task_id,
    )
    state._project_dir_override = str(project_dir)
    record_validation_verdict(
        db_session,
        session_id=session_id,
        task_id=task_id,
        orchestration_state=state,
        verdict=outcome.verdict,
    )
    db_session.commit()

    # Discard every in-memory handle on the verdict, then reload through a fresh
    # SQLAlchemy session — the closest deterministic analogue of a worker restart.
    expected_identity = in_memory.authority_identity
    expected_grants = tuple(grant.payload() for grant in in_memory.grants)
    del outcome, in_memory

    reloaded_session = db_session_factory()
    try:
        checkpoint = (
            reloaded_session.query(TaskCheckpoint)
            .filter(
                TaskCheckpoint.task_id == task_id,
                TaskCheckpoint.checkpoint_type == "validation_plan",
            )
            .one()
        )
        snapshot = json.loads(checkpoint.state_snapshot)
    finally:
        reloaded_session.close()

    restored = accepted_path_authority_from_verdict(snapshot)
    assert restored is not None
    assert restored.authority_identity == expected_identity
    assert tuple(grant.payload() for grant in restored.grants) == expected_grants
    # The sibling source-materialization record is preserved, not replaced.
    assert "source_materialization" in snapshot["details"]
    assert snapshot["details"]["source_materialization"]["files"]


def test_tampered_persisted_authority_is_rejected_on_reload(tmp_path):
    authority = _authority_for(workspace_identity=str(tmp_path))
    payload = authority.to_dict()
    payload["grants"][0]["grant_class"] = GrantClass.EXISTING_MUTABLE.value

    with pytest.raises(PathGrantError) as excinfo:
        accepted_path_authority_from_verdict(
            {"details": {"accepted_path_authority": payload}}
        )
    assert excinfo.value.code == "authority_identity_mismatch"


def test_reader_returns_none_when_no_authority_was_recorded():
    assert accepted_path_authority_from_verdict({"details": {}}) is None
    assert accepted_path_authority_from_verdict({}) is None


def test_source_materialization_metadata_is_kept_as_a_sibling(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "money.py").write_text("VALUE = 1\n", encoding="utf-8")

    outcome = _validate(
        _read_only_plan(),
        project_dir=tmp_path,
        task_prompt="Review the repository and report test status.",
        execution_profile="review_only",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _existing_record(
                    "app/money.py",
                    workspace_identity=str(tmp_path),
                    content_hash=_digest("money"),
                ),
            ),
        ),
    )
    assert isinstance(outcome, PlanAccepted)
    materialization = outcome.details["source_materialization"]
    assert materialization["workspace_identity"] == str(tmp_path)
    assert materialization["files"][0]["relative_path"] == "app/money.py"
    assert outcome.details["accepted_path_authority"]["workspace_identity"] == str(
        tmp_path
    )


def test_authority_is_absent_and_explained_when_source_grounding_is_unavailable():
    outcome = _validate(
        _read_only_plan(),
        project_dir=None,
        task_prompt="Review the repository and report test status.",
        execution_profile="review_only",
    )
    assert isinstance(outcome, PlanAccepted), outcome.reasons
    assert "accepted_path_authority" not in outcome.details
    assert (
        outcome.details["accepted_path_authority_unavailable"]
        == "source_materialization_absent"
    )


# ---------------------------------------------------------------------------
# zero-consumer proof
# ---------------------------------------------------------------------------


CONSTRUCTION_CALLER_ALLOWLIST = {
    "orchestration/validation/accepted_path_authority.py",
    "orchestration/validation/validator.py",
}

DOWNSTREAM_MODULES = (
    "orchestration/execution/executor.py",
    "orchestration/phases/execution_loop.py",
    "orchestration/validation/workspace_guard.py",
    "orchestration/validation/candidate_checks.py",
    "orchestration/phases/completion_repair.py",
    "orchestration/phases/completion_flow.py",
    "orchestration/coordinators/completion_coordinator.py",
    "workspace/changeset_service.py",
    "workspace/baseline_promotion_service.py",
)


def _production_sources() -> list[tuple[str, str]]:
    return [
        (
            path.relative_to(PRODUCTION_ROOT).as_posix(),
            path.read_text(encoding="utf-8"),
        )
        for path in sorted(PRODUCTION_ROOT.rglob("*.py"))
    ]


def test_authority_is_constructed_only_by_plan_validation():
    callers = {
        relative
        for relative, text in _production_sources()
        if "build_accepted_path_authority" in text
    }
    assert callers == CONSTRUCTION_CALLER_ALLOWLIST


def test_no_downstream_stage_reads_the_authority():
    sources = dict(_production_sources())
    for relative in DOWNSTREAM_MODULES:
        text = sources[relative]
        assert "AcceptedPathAuthority" not in text, relative
        assert "accepted_path_authority_from_verdict" not in text, relative
        assert "build_accepted_path_authority" not in text, relative
        assert '"accepted_path_authority"' not in text, relative


def test_completion_coordinator_only_reuses_the_plan_identity_helper():
    sources = dict(_production_sources())
    text = sources["orchestration/coordinators/completion_coordinator.py"]
    assert "plan_identity_text" in text
    assert "accepted_path_authority import (\n    plan_identity_text,\n)" in text


def test_reader_helper_has_no_production_callers():
    callers = {
        relative
        for relative, text in _production_sources()
        if "accepted_path_authority_from_verdict" in text
        and relative != "orchestration/validation/accepted_path_authority.py"
    }
    assert callers == set()
