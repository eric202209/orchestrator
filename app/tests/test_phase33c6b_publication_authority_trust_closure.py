"""Phase 33C-6B publication authority, declaration, and destination safety."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.state.persistence import load_accepted_path_authority
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathAuthorityError,
    PathGrant,
    TrustClass,
    classify_trust,
    declare,
    publication_scope_violations,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.workspace.baseline_promotion_service import BaselinePromotionService


_HASH = "0" * 64


def _plan(path: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": f"Publish {path}",
            "ops": [{"op": "write_file", "path": path}],
            "expected_files": [path],
        }
    ]


def _authority(plan: list[dict], workspace: Path, grants: list[tuple[str, GrantClass]]):
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(workspace.resolve()),
        maximum_scope_digest=_HASH,
        grants=[
            PathGrant(
                path=declare(path),
                grant_class=grant_class,
                provenance=GrantProvenance.ACCEPTED_PLAN,
                baseline_content_hash=(
                    _HASH if grant_class is not GrantClass.CREATION_AUTHORIZED else None
                ),
            )
            for path, grant_class in grants
        ],
    )


def _seed_publication(
    db_session,
    tmp_path: Path,
    *,
    plan: list[dict],
    grants: list[tuple[str, GrantClass]],
    change_set: dict,
    persist_authority: bool = True,
    validated_identity: str | None = None,
):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True)
    project = Project(name="C6B publication", workspace_path=str(project_root))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="C6B session",
        status="running",
        is_active=True,
        execution_mode="automatic",
    )
    db_session.add(session)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="C6B task",
        description="Publication trust closure",
        status=TaskStatus.DONE,
        steps=json.dumps(plan),
    )
    db_session.add(task)
    db_session.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()

    authority = _authority(plan, tmp_path / "runtime", grants)
    if persist_authority:
        db_session.add(
            TaskCheckpoint(
                task_id=task.id,
                session_id=session.id,
                checkpoint_type="validation_plan",
                state_snapshot=json.dumps(
                    {
                        "stage": "plan",
                        "status": "accepted",
                        "details": {"accepted_path_authority": authority.to_dict()},
                    }
                ),
            )
        )

    change_set = {
        "project_id": project.id,
        "task_id": task.id,
        "session_id": session.id,
        "task_execution_id": execution.id,
        **change_set,
    }
    artifact = project_root / ".agent" / "change-sets" / str(execution.id) / "files"
    artifact.mkdir(parents=True)
    for relative in set(change_set.get("added_files") or []) | set(
        change_set.get("modified_files") or []
    ):
        path = artifact / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("candidate\n", encoding="utf-8")

    if persist_authority:
        identity = validated_identity or candidate_delta_identity(
            change_set, project_dir=artifact
        )
        db_session.add(
            TaskCheckpoint(
                task_id=task.id,
                session_id=session.id,
                checkpoint_type="validation_task_completion",
                state_snapshot=json.dumps(
                    {
                        "stage": "task_completion",
                        "status": "accepted",
                        "candidate_identity": identity,
                    }
                ),
            )
        )
    db_session.commit()
    return project, task, execution, authority, change_set, artifact


def _promote(db_session, project, task, change_set):
    return BaselinePromotionService(
        db_session
    ).promote_change_set_into_baseline_unlocked(project, task, change_set)


def test_valid_modified_file_requires_existing_mutable_and_publishes(
    db_session, tmp_path
):
    plan = _plan("app/real.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
    )
    (Path(project.workspace_path) / "app").mkdir()
    (Path(project.workspace_path) / "app" / "real.py").write_text("baseline\n")

    result = _promote(db_session, project, task, change_set)

    assert result["files_copied"] == 1
    assert (
        Path(project.workspace_path) / "app" / "real.py"
    ).read_text() == "candidate\n"
    assert result["publication_authority"]["authority_identity"]


def test_valid_creation_requires_creation_authorized(db_session, tmp_path):
    plan = _plan("app/new.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/new.py", GrantClass.CREATION_AUTHORIZED)],
        change_set={
            "added_files": ["app/new.py"],
            "modified_files": [],
            "deleted_files": [],
        },
    )

    result = _promote(db_session, project, task, change_set)

    assert result["files_copied"] == 1
    assert (Path(project.workspace_path) / "app" / "new.py").exists()


@pytest.mark.parametrize(
    ("grant_class", "operation"),
    [
        (GrantClass.EXISTING_READONLY, "modified_files"),
        (GrantClass.CREATION_AUTHORIZED, "modified_files"),
    ],
)
def test_wrong_grant_class_fails_closed_before_copy(
    db_session, tmp_path, grant_class, operation
):
    path = "app/readonly.py"
    plan = _plan(path)
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[(path, grant_class)],
        change_set={"added_files": [], "modified_files": [path], "deleted_files": []},
    )
    (Path(project.workspace_path) / "app").mkdir()
    (Path(project.workspace_path) / "app" / "readonly.py").write_text("baseline\n")

    with pytest.raises(PathAuthorityError, match="publication_scope_violation"):
        _promote(db_session, project, task, change_set)
    assert (
        Path(project.workspace_path) / "app" / "readonly.py"
    ).read_text() == "baseline\n"


def test_unauthorized_observed_path_is_not_copied(db_session, tmp_path):
    plan = _plan("app/allowed.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/allowed.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/invented.py"],
            "deleted_files": [],
        },
    )

    with pytest.raises(PathAuthorityError, match="publication_scope_violation"):
        _promote(db_session, project, task, change_set)
    assert not (Path(project.workspace_path) / "app" / "invented.py").exists()


def test_unauthorized_deletion_is_rejected_without_inference(db_session, tmp_path):
    path = "app/deleted.py"
    plan = _plan(path)
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[(path, GrantClass.EXISTING_MUTABLE)],
        change_set={"added_files": [], "modified_files": [], "deleted_files": [path]},
    )
    target = Path(project.workspace_path) / path
    target.parent.mkdir(parents=True)
    target.write_text("must remain\n")

    with pytest.raises(PathAuthorityError, match="publication_scope_violation"):
        _promote(db_session, project, task, change_set)
    assert target.read_text() == "must remain\n"


def test_case_alias_is_rejected_independently_of_host_filesystem(db_session, tmp_path):
    plan = _plan("App/Real.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("App/Real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
    )

    with pytest.raises(PathAuthorityError, match="publication_scope_violation"):
        _promote(db_session, project, task, change_set)


def test_wrong_plan_lineage_fails_before_publication(db_session, tmp_path):
    accepted_plan = _plan("app/accepted.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=_plan("app/current.py"),
        grants=[("app/accepted.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/accepted.py"],
            "deleted_files": [],
        },
    )
    task.steps = json.dumps(accepted_plan)
    db_session.commit()

    with pytest.raises(PathAuthorityError, match="authority_plan_identity_mismatch"):
        _promote(db_session, project, task, change_set)


def test_wrong_candidate_identity_fails_before_copy(db_session, tmp_path):
    plan = _plan("app/real.py")
    project, task, _, _, change_set, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
        validated_identity="sha256:" + "f" * 64,
    )
    (Path(project.workspace_path) / "app").mkdir()
    (Path(project.workspace_path) / "app" / "real.py").write_text("baseline\n")

    with pytest.raises(PathAuthorityError, match="candidate_identity_unvalidated"):
        _promote(db_session, project, task, change_set)
    assert (
        Path(project.workspace_path) / "app" / "real.py"
    ).read_text() == "baseline\n"


def test_missing_and_tampered_authority_fail_closed(db_session, tmp_path):
    plan = _plan("app/real.py")
    project, task, _, authority, change_set, _ = _seed_publication(
        db_session,
        tmp_path / "missing",
        plan=plan,
        grants=[("app/real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
        persist_authority=False,
    )
    with pytest.raises(PathAuthorityError, match="authority_record_missing"):
        _promote(db_session, project, task, change_set)

    tamper_root = tmp_path / "tampered"
    project, task, execution, authority, change_set, _ = _seed_publication(
        db_session,
        tamper_root,
        plan=plan,
        grants=[("app/real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
    )
    checkpoint = (
        db_session.query(TaskCheckpoint)
        .filter(
            TaskCheckpoint.task_id == task.id,
            TaskCheckpoint.checkpoint_type == "validation_plan",
        )
        .one()
    )
    payload = json.loads(checkpoint.state_snapshot)
    payload["details"]["accepted_path_authority"]["authority_identity"] = "0" * 64
    checkpoint.state_snapshot = json.dumps(payload)
    db_session.commit()

    with pytest.raises(PathAuthorityError, match="authority_identity_mismatch"):
        _promote(db_session, project, task, change_set)


def test_publication_loader_binds_task_session_plan_and_authority_identity(
    db_session, tmp_path
):
    plan = _plan("app/real.py")
    project, task, execution, authority, _, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/real.py", GrantClass.EXISTING_MUTABLE)],
        change_set={"added_files": [], "modified_files": [], "deleted_files": []},
    )

    loaded = load_accepted_path_authority(
        db_session,
        task_id=task.id,
        session_id=execution.session_id,
        task_execution_id=execution.id,
        plan=plan,
        workspace_identity=None,
    )

    assert loaded.authority_identity == authority.authority_identity
    assert loaded.accepted_plan_identity == accepted_plan_identity(plan)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../outside.py",
        "C:/outside.py",
        r"app\\real.py",
        "https://example.test/file.py",
        "app//real.py",
        "app/real.py/",
        "app/\x00real.py",
    ],
)
def test_publication_declarations_use_canonical_contract(raw):
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity="plan",
        workspace_identity="workspace",
        maximum_scope_digest=_HASH,
    )
    violations = publication_scope_violations(authority, modified_paths=[raw])

    assert violations
    assert violations[0]["code"] == "publication_declaration_invalid"


def test_toolchain_declaration_is_lexically_valid_but_not_publishable():
    declared = declare("venv/bin/python3")
    assert declared.value == "venv/bin/python3"
    assert classify_trust(declared) is TrustClass.TRUSTED_TOOLCHAIN
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity="plan",
        workspace_identity="workspace",
        maximum_scope_digest=_HASH,
    )
    assert (
        publication_scope_violations(authority, added_paths=[declared.value])[0]["code"]
        == "publication_non_product_path"
    )


def test_publication_scope_pure_check_reports_operation_and_alias_mismatches():
    authority = _authority(
        _plan("App/Real.py"),
        Path("/authority-workspace"),
        [("App/Real.py", GrantClass.EXISTING_MUTABLE)],
    )
    violations = publication_scope_violations(
        authority,
        added_paths=["App/Real.py"],
        modified_paths=["app/real.py"],
        deleted_paths=["App/Real.py"],
    )

    assert {item["code"] for item in violations} >= {
        "publication_grant_class_mismatch",
        "publication_path_case_alias",
    }


def _copy_fixture(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "source"
    target = root / "target"
    source.mkdir()
    target.mkdir()
    promoter = BaselinePromotionService(None)
    promoter.get_project_root = lambda _project: root
    promoter.get_project_tasks = lambda _project_id: []
    return promoter, source, target, SimpleNamespace(id=1)


def test_whole_workspace_intermediate_symlink_is_detected_before_copy(tmp_path):
    promoter, source, target, project = _copy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "passwd"
    victim.write_text("original\n")
    (target / "some").symlink_to(outside, target_is_directory=True)
    (source / "some").mkdir()
    (source / "some" / "passwd").write_text("attacker\n")

    with pytest.raises(PathAuthorityError, match="publication_destination_symlink"):
        promoter._copy_tree_into_target(project, source, target, True)
    assert victim.read_text() == "original\n"


def test_whole_workspace_final_symlink_is_detected_before_copy(tmp_path):
    promoter, source, target, project = _copy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "file.txt"
    victim.write_text("original\n")
    (target / "file.txt").symlink_to(victim)
    (source / "file.txt").write_text("attacker\n")

    with pytest.raises(PathAuthorityError, match="publication_destination_symlink"):
        promoter._copy_tree_into_target(project, source, target, True)
    assert victim.read_text() == "original\n"


def test_whole_workspace_copy_is_normal_and_excluded_content_stays_excluded(tmp_path):
    promoter, source, target, project = _copy_fixture(tmp_path)
    (source / "app").mkdir()
    (source / "app" / "real.py").write_text("content\n")
    (source / "venv" / "bin").mkdir(parents=True)
    (source / "venv" / "bin" / "python3").write_text("toolchain\n")

    assert promoter._copy_tree_into_target(project, source, target, True) == 1
    assert (target / "app" / "real.py").read_text() == "content\n"
    assert not (target / "venv" / "bin" / "python3").exists()


def test_whole_workspace_publication_uses_persisted_apa(db_session, tmp_path):
    plan = _plan("app/new.py")
    project, task, _, _, _, _ = _seed_publication(
        db_session,
        tmp_path,
        plan=plan,
        grants=[("app/new.py", GrantClass.CREATION_AUTHORIZED)],
        change_set={"added_files": [], "modified_files": [], "deleted_files": []},
    )
    task.task_subfolder = "task-work"
    source = Path(project.workspace_path) / task.task_subfolder / "app"
    source.mkdir(parents=True)
    (source / "new.py").write_text("print('new')\n", encoding="utf-8")
    db_session.commit()

    result = BaselinePromotionService(db_session).promote_task_into_baseline(
        project, task
    )

    assert result["files_copied"] == 1
    assert (Path(project.workspace_path) / "app" / "new.py").read_text(
        encoding="utf-8"
    ) == "print('new')\n"


def test_whole_workspace_unsafe_destination_has_zero_partial_apply(tmp_path):
    promoter, source, target, project = _copy_fixture(tmp_path)
    (source / "safe.py").write_text("safe\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "unsafe").symlink_to(outside, target_is_directory=True)
    (source / "unsafe").mkdir()
    (source / "unsafe" / "file.py").write_text("unsafe\n")

    with pytest.raises(PathAuthorityError, match="publication_destination_symlink"):
        promoter._copy_tree_into_target(project, source, target, True)
    assert not (target / "safe.py").exists()
    assert not (outside / "file.py").exists()


def test_publication_code_does_not_construct_or_persist_authority():
    production = Path(__file__).parents[1] / "services"
    publication_files = [
        production / "workspace" / "baseline_promotion_service.py",
        production / "orchestration" / "validation" / "validator.py",
        production / "orchestration" / "coordinators" / "completion_coordinator.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in publication_files)
    assert source.count("PathGrant(") == 0
    assert source.count("AcceptedPathAuthority.create(") == 0


def test_preflight_consumes_apa_and_rejects_attempt_16_shape(tmp_path):
    baseline = tmp_path / "baseline"
    (baseline / "frontend" / "src" / "pages").mkdir(parents=True)
    (baseline / "frontend" / "src" / "pages" / "SessionDetail.tsx").write_text("A")
    plan = _plan("frontend/src/pages/SessionDetail.tsx")
    authority = _authority(
        plan,
        tmp_path,
        [("frontend/src/pages/SessionDetail.tsx", GrantClass.EXISTING_MUTABLE)],
    )

    verdict = ValidatorService.validate_baseline_publish(
        validation_profile="implementation",
        baseline_path=str(baseline),
        baseline_file_count=1,
        missing_task_expected_files=[],
        missing_prior_expected_files=[],
        candidate_change_set={
            "added_files": [],
            "modified_files": ["frontend/src/components/session/SessionDetail.tsx"],
            "deleted_files": [],
        },
        accepted_path_authority=authority,
        require_accepted_path_authority=True,
        validated_candidate_identity="sha256:" + "0" * 64,
    )

    assert verdict.status == "rejected"
    assert "publication_authority_violations" in verdict.details
