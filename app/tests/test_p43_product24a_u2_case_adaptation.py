"""P43-PRODUCT-24A: bounded U1/U2 case adaptation (provider-free)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.models import Project, Session as SessionModel, Task
from app.tests import test_p43_product22_c2_c3b_live_execution as c3b
from app.tests import test_p43_product23b_trusted_controls as c23b
from app.tests import test_p43_product23c_real_provider_evaluation as c23c
from scripts.maintenance import p43_c2_trusted_submitter as submitter


EVIDENCE = c23b.EVIDENCE
SUBMITTER = c23b.SUBMITTER
LIVE_RUN = c23b.LIVE_RUN
c2_env = c23c.c2_env


def _require_u2_evidence() -> None:
    if not (EVIDENCE / submitter.U2_DIR / "manifest.json").is_file():
        pytest.skip("frozen PRODUCT-18 U2 evidence is not present (docs/ is local)")


def _stage(tmp_path: Path, parts: str, case_id: str) -> Path:
    _require_u2_evidence()
    destination = tmp_path / f"material-{case_id.lower()}-{parts}"
    result = subprocess.run(
        [
            "python3",
            "-I",
            str(SUBMITTER),
            "stage-material",
            str(EVIDENCE),
            str(destination),
            "--parts",
            parts,
            "--case",
            case_id,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return destination


def test_case_descriptor_accepts_only_frozen_u1_and_u2():
    assert submitter.frozen_case("U1").manifest_sha256 == submitter.U1_MANIFEST_SHA256
    assert (
        submitter.frozen_case("U2").manifest_sha256
        == "314142190e698daf19b1b2f3e8d7e64629c9c7644953cb22442458569ee1bd3d"
    )
    with pytest.raises(submitter.SubmitterRefused, match="unknown_case"):
        submitter.frozen_case("U9")


def test_u2_material_is_pinned_and_cross_case_substitution_is_refused(tmp_path):
    u2 = _stage(tmp_path, "evaluation", "U2")
    manifest = submitter.verify_case_material(u2, submitter.EVALUATION_PARTS, "U2")
    assert manifest["id"] == "U2"
    assert manifest["acceptance"] == "tests/test_ledgerlite_acceptance.py"
    u1 = c23b._stage(tmp_path, "evaluation")
    with pytest.raises(submitter.SubmitterRefused, match="u2_manifest_missing"):
        submitter.verify_case_material(u1, submitter.EVALUATION_PARTS, "U2")
    with pytest.raises(submitter.SubmitterRefused, match="u1_manifest_missing"):
        submitter.verify_case_material(u2, submitter.EVALUATION_PARTS, "U1")
    tampered = _stage(tmp_path, "model", "U2")
    manifest_path = tampered / submitter.U2_DIR / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + " ")
    with pytest.raises(submitter.SubmitterRefused, match="u2_manifest_sha256_mismatch"):
        submitter.verify_case_material(tampered, submitter.MODEL_PARTS, "U2")


def test_u2_model_mount_contains_only_manifest_task_and_baseline(tmp_path):
    material = _stage(tmp_path, "model", "U2")
    manifest = submitter.verify_case_material(material, submitter.MODEL_PARTS, "U2")
    staged = {
        path.relative_to(material).as_posix()
        for path in material.rglob("*")
        if path.is_file()
    }
    assert staged == {
        "p43-product-18/u2/manifest.json",
        "p43-product-18/u2/task.txt",
    } | {
        f"p43-product-18/u2/baseline/{relative}"
        for relative in manifest["baseline_files"]
    }
    assert not (material / submitter.U2_DIR / "controls").exists()
    assert not (material / submitter.U2_DIR / "hidden").exists()
    assert not (material / submitter.ORACLE_PATH).exists()


def test_u2_submission_uses_frozen_task_and_baseline_only(
    c2_env, monkeypatch, db_session_factory, tmp_path
):
    material = _stage(tmp_path, "model", "U2")
    monkeypatch.setattr(submitter, "U2_MATERIAL_ROOT", str(material))
    queued = []

    def queue(db, session, task_id, timeout_seconds):
        from app.models import TaskExecution, TaskStatus

        execution = TaskExecution(
            task_id=task_id,
            session_id=session.id,
            attempt_number=1,
            status=TaskStatus.PENDING,
        )
        db.add(execution)
        db.commit()
        queued.append(timeout_seconds)
        return {"task_execution_id": execution.id, "status": "queued"}

    monkeypatch.setattr(
        "app.services.session.session_runtime_service.queue_task_for_session", queue
    )
    result = submitter.submit(
        db_session_factory,
        control="model",
        case_id="U2",
    )
    manifest = json.loads(
        (material / submitter.U2_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert result["case_id"] == "U2"
    assert result["case_manifest_sha256"] == submitter.U2_MANIFEST_SHA256
    assert set(result["seeded_files"]) == set(manifest["baseline_files"])
    assert queued == [submitter.MODEL_TIMEOUT_SECONDS]
    db = db_session_factory()
    try:
        task = db.get(Task, result["task_id"])
        project = db.get(Project, result["project_id"])
        session = db.get(SessionModel, result["session_id"])
        assert project.name == submitter._control_project_names("U2")["model"]
        assert (
            task.description == (material / submitter.U2_DIR / "task.txt").read_text()
        )
        assert task.steps is None
        assert task.execution_profile == "full_lifecycle"
        assert session.execution_mode == "manual"
    finally:
        db.close()


def _u2_evaluation_only_line(evaluation: Path) -> str:
    manifest = submitter.verify_case_material(
        evaluation, submitter.EVALUATION_PARTS, "U2"
    )
    visible = set(submitter._material_files(manifest, submitter.MODEL_PARTS, "U2"))
    allowed = "\n".join(
        (evaluation / relative).read_text(encoding="utf-8")
        for relative in sorted(visible)
    )
    for relative in sorted(
        set(submitter._material_files(manifest, submitter.EVALUATION_PARTS, "U2"))
        - visible
    ):
        for line in (evaluation / relative).read_text(encoding="utf-8").splitlines():
            if len(line.strip()) >= 24 and line.strip() not in allowed:
                return line.strip()
    raise AssertionError("U2 evaluation-only marker was not found")


def _relay_bodies(tmp_path: Path, exchanges: list[tuple[str, str | None]]) -> Path:
    state = tmp_path / "relay"
    bodies = state / "bodies"
    bodies.mkdir(parents=True)
    for index, (request, response) in enumerate(exchanges, start=1):
        payload = {
            "model": "qwen-local",
            "messages": [{"role": "user", "content": request}],
        }
        (bodies / f"{index:04d}.request.json").write_text(json.dumps(payload))
        if response is not None:
            answer = {"choices": [{"message": {"content": response}}]}
            (bodies / f"{index:04d}.response.json").write_text(json.dumps(answer))
    return state


def test_u2_contamination_scan_is_bound_to_u2_material(tmp_path):
    evaluation = _stage(tmp_path, "evaluation", "U2")
    task = (evaluation / submitter.U2_DIR / "task.txt").read_text(encoding="utf-8")
    marker = _u2_evaluation_only_line(evaluation)
    clean = submitter.provider_request_contamination(
        evaluation,
        _relay_bodies(tmp_path / "clean", [(task, "candidate patch")]),
        "U2",
    )
    assert clean["case_id"] == "U2"
    assert clean["classification"] == "NO_EVALUATION_MATERIAL_IN_REQUESTS"
    leaked = submitter.provider_request_contamination(
        evaluation,
        _relay_bodies(tmp_path / "leaked", [(task + marker, None)]),
        "U2",
    )
    assert leaked["case_manifest_sha256"] == submitter.U2_MANIFEST_SHA256
    assert leaked["classification"] == "REVIEW_REQUIRED"
    assert leaked["unexplained_hits"] >= 1


def test_runner_evaluation_paths_are_manifest_derived_and_case_bound():
    _require_u2_evidence()
    block = c23b._shell_function(LIVE_RUN, "cmd_evaluate")
    manifest = json.loads(
        (EVIDENCE / submitter.U2_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert [manifest["acceptance"], *manifest["existing_tests"]] == [
        "tests/test_ledgerlite_acceptance.py",
        "tests/test_ledger.py",
    ]
    assert 'manifest["acceptance"]' in block
    assert 'manifest["existing_tests"]' in block
    assert "test_rangekit_acceptance.py" not in block
    assert "tests/test_ranges.py" not in block
    assert 'verdict "$evaluation_root" "$cs"' in block
    assert '--case "$case_id"' in block


def test_u2_change_set_export_provenance_preserves_case_identity(tmp_path):
    result, _, evidence, _ = c23c._run_change_set_export(tmp_path, case_id="U2")
    assert result.returncode == 0, result.stderr
    provenance = json.loads(
        (evidence / "change-sets" / "model" / "export-provenance.json").read_text()
    )
    assert provenance["case_id"] == "U2"
    assert provenance["case_manifest_sha256"] == submitter.U2_MANIFEST_SHA256
