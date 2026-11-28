"""P43-PRODUCT-25A: bounded U3 Pagekit case adaptation (provider-free)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.tests import test_p43_product23b_trusted_controls as c23b
from app.tests import test_p43_product23c_real_provider_evaluation as c23c
from app.tests import test_p43_product24a_u2_case_adaptation as c24a
from app.tests import test_p43_product24b_r1_no_change_set_case_binding as c24br1
from scripts.maintenance import p43_c2_trusted_submitter as submitter


EVIDENCE = c23b.EVIDENCE
LIVE_RUN = c23b.LIVE_RUN
c2_env = c23c.c2_env
U3_MANIFEST = "1b67408c8253a2d4c2d91e47ec9e5c552f0052e820869144813dab8828a89911"


def _stage(tmp_path: Path, parts: str, case_id: str = "U3") -> Path:
    if not (EVIDENCE / submitter.U3_DIR / "manifest.json").is_file():
        pytest.skip("frozen PRODUCT-18 U3 evidence is not present (docs/ is local)")
    return c24a._stage(tmp_path, parts, case_id)


def test_u3_descriptor_is_pinned_and_unknown_cases_still_refuse():
    case = submitter.frozen_case("U3")
    assert case.manifest_sha256 == U3_MANIFEST
    assert case.directory == "p43-product-18/u3"
    assert case.evaluation_controls == ("reference", "naive-clamp", "naive-unexported")
    assert submitter.case_info("U3")["material_root"] == "/run/p43-u3-material"
    assert submitter.case_info("U1")["material_root"] == submitter.U1_MATERIAL_ROOT
    assert submitter.case_info("U2")["material_root"] == submitter.U2_MATERIAL_ROOT
    with pytest.raises(submitter.SubmitterRefused, match="unknown_case"):
        submitter.frozen_case("U4")


def test_u3_material_is_pinned_and_cross_case_substitution_is_refused(tmp_path):
    u3 = _stage(tmp_path, "evaluation")
    manifest = submitter.verify_case_material(u3, submitter.EVALUATION_PARTS, "U3")
    assert manifest["acceptance"] == "tests/test_page_window_acceptance.py"
    assert manifest["existing_tests"] == [
        "tests/test_paging.py",
        "tests/test_partial_page_regression.py",
    ]
    u2 = _stage(tmp_path, "evaluation", "U2")
    with pytest.raises(submitter.SubmitterRefused, match="u3_manifest_missing"):
        submitter.verify_case_material(u2, submitter.EVALUATION_PARTS, "U3")
    with pytest.raises(submitter.SubmitterRefused, match="u2_manifest_missing"):
        submitter.verify_case_material(u3, submitter.EVALUATION_PARTS, "U2")
    tampered = _stage(tmp_path, "model")
    links = tampered / submitter.U3_DIR / "baseline" / "pagekit" / "links.py"
    links.write_text(links.read_text(encoding="utf-8") + "\n")
    with pytest.raises(submitter.SubmitterRefused, match="u3_material_mismatch"):
        submitter.verify_case_material(tampered, submitter.MODEL_PARTS, "U3")


def test_u3_model_mount_contains_only_manifest_task_and_baseline(tmp_path):
    material = _stage(tmp_path, "model")
    manifest = submitter.verify_case_material(material, submitter.MODEL_PARTS, "U3")
    staged = {
        path.relative_to(material).as_posix()
        for path in material.rglob("*")
        if path.is_file()
    }
    assert staged == {
        "p43-product-18/u3/manifest.json",
        "p43-product-18/u3/task.txt",
    } | {
        f"p43-product-18/u3/baseline/{relative}"
        for relative in manifest["baseline_files"]
    }
    assert len(staged) == 8
    assert not (material / submitter.U3_DIR / "controls").exists()
    assert not (material / submitter.U3_DIR / "hidden").exists()
    assert not (material / submitter.ORACLE_PATH).exists()


def test_u3_evaluation_mount_stages_only_selected_controls(tmp_path):
    material = _stage(tmp_path, "evaluation")
    controls = sorted(
        path.name for path in (material / submitter.U3_DIR / "controls").iterdir()
    )
    assert controls == ["naive-clamp", "naive-unexported", "reference"]
    assert sum(1 for path in material.rglob("*") if path.is_file()) == 23


def test_u3_submission_uses_frozen_task_and_baseline_only(
    c2_env, monkeypatch, db_session_factory, tmp_path
):
    from app.models import Project, Task, TaskExecution, TaskStatus

    material = _stage(tmp_path, "model")
    monkeypatch.setattr(submitter, "U3_MATERIAL_ROOT", str(material))
    queued = []

    def queue(db, session, task_id, timeout_seconds):
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
    result = submitter.submit(db_session_factory, control="model", case_id="U3")
    manifest = json.loads(
        (material / submitter.U3_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert result["case_id"] == "U3"
    assert result["case_manifest_sha256"] == U3_MANIFEST
    assert set(result["seeded_files"]) == set(manifest["baseline_files"])
    assert queued == [submitter.MODEL_TIMEOUT_SECONDS]
    db = db_session_factory()
    try:
        task = db.get(Task, result["task_id"])
        project = db.get(Project, result["project_id"])
        assert project.name == submitter._control_project_names("U3")["model"]
        assert (
            task.description == (material / submitter.U3_DIR / "task.txt").read_text()
        )
        assert task.steps is None
    finally:
        db.close()


def test_u3_contamination_scan_is_bound_to_u3_material(tmp_path):
    evaluation = _stage(tmp_path, "evaluation")
    task = (evaluation / submitter.U3_DIR / "task.txt").read_text(encoding="utf-8")
    manifest = submitter.verify_case_material(
        evaluation, submitter.EVALUATION_PARTS, "U3"
    )
    visible = set(submitter._material_files(manifest, submitter.MODEL_PARTS, "U3"))
    allowed = "\n".join((evaluation / rel).read_text() for rel in sorted(visible))
    hidden = evaluation / submitter.U3_DIR / "hidden" / "test_u3_hidden_preservation.py"
    marker = next(
        line.strip()
        for line in hidden.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) >= 24 and line.strip() not in allowed
    )
    clean = submitter.provider_request_contamination(
        evaluation,
        c24a._relay_bodies(tmp_path / "clean", [(task, "candidate patch")]),
        "U3",
    )
    assert clean["case_id"] == "U3"
    assert clean["classification"] == "NO_EVALUATION_MATERIAL_IN_REQUESTS"
    leaked = submitter.provider_request_contamination(
        evaluation,
        c24a._relay_bodies(tmp_path / "leaked", [(task + marker, None)]),
        "U3",
    )
    assert leaked["case_manifest_sha256"] == U3_MANIFEST
    assert leaked["classification"] == "REVIEW_REQUIRED"


def test_runner_selects_u3_slug_and_refuses_unknown_cases():
    function = c23b._shell_function(LIVE_RUN, "case_slug")
    prelude = 'refuse() { echo "REFUSED $*" >&2; exit 78; }\n' + function
    ok = subprocess.run(
        ["bash", "-c", prelude + "case_slug U3"], capture_output=True, text=True
    )
    assert (ok.returncode, ok.stdout) == (0, "u3\n")
    bad = subprocess.run(
        ["bash", "-c", prelude + "case_slug U4"], capture_output=True, text=True
    )
    assert bad.returncode == 78


def test_u3_change_set_export_provenance_preserves_case_identity(tmp_path):
    result, _, evidence, _ = c23c._run_change_set_export(tmp_path, case_id="U3")
    assert result.returncode == 0, result.stderr
    provenance = json.loads(
        (evidence / "change-sets" / "model" / "export-provenance.json").read_text()
    )
    assert provenance["case_id"] == "U3"
    assert provenance["case_manifest_sha256"] == U3_MANIFEST


def test_failed_u3_task_without_change_set_keeps_u3_provenance(tmp_path):
    root = tmp_path / "root"
    (root / "env").mkdir(parents=True)
    (root / "u3-eval").mkdir()
    (root / "relay").mkdir()
    (root / "env" / "run.vars").write_text("CASE_ID=U3\n", encoding="utf-8")
    (root / "env" / "observe.model.json").write_text(
        json.dumps(c24br1._observation("U3", U3_MANIFEST)), encoding="utf-8"
    )
    stub = tmp_path / "submitter_stub.py"
    stub.write_text("print('{}')\n", encoding="utf-8")
    command = (
        "set -euo pipefail\n"
        f"ROOT={root}\nSUBMITTER={stub}\n"
        'refuse() { echo "REFUSED $*" >&2; exit 1; }\n'
        'bad() { echo "FAIL $*" >&2; }\n'
        "require_host() { :; }\nprovider_mode() { :; }\n"
        + c23b._shell_function(LIVE_RUN, "selected_case")
        + c23b._shell_function(LIVE_RUN, "case_slug")
        + c23b._shell_function(LIVE_RUN, "cmd_evaluate")
        + "cmd_evaluate model\n"
    )
    result = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    verdict = json.loads((root / "eval" / "model" / "verdict.json").read_text())
    assert verdict == {
        "control": "model",
        "verdict": "NO_CHANGE_SET",
        "case_id": "U3",
        "case_manifest_sha256": U3_MANIFEST,
    }


def _change_set(tmp_path: Path, files: dict[str, str]) -> Path:
    change_set = tmp_path / "change-set"
    for rel, text in files.items():
        target = change_set / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (change_set / "manifest.json").write_text(
        json.dumps({"copied_files": sorted(files), "deleted_files": []})
    )
    return change_set


@pytest.mark.parametrize(
    "control, classification",
    [
        ("reference", "EFFECTIVE"),
        ("naive-clamp", "CANDIDATE_FAILED"),
        ("ineffective-test", "INEFFECTIVE"),
    ],
)
def test_u3_regression_probe_requires_assertion_failure_on_declared_stub(
    tmp_path, control, classification
):
    evaluation = _stage(tmp_path, "evaluation")
    control_dir = EVIDENCE / submitter.U3_DIR / "controls" / control
    result = submitter.regression_effectiveness(evaluation, control_dir, "U3")
    assert result["baseline_probe"] == "complete_baseline_with_absent_symbol_stub"
    assert result["classification"] == classification
    if control == "reference":
        assert set(result["baseline"]["cases"].values()) == {"assertion_failure"}


def test_u3_import_only_regression_test_is_not_effective(tmp_path):
    evaluation = _stage(tmp_path, "evaluation")
    reference = EVIDENCE / submitter.U3_DIR / "controls" / "reference" / "files"
    files = {
        rel: (reference / rel).read_text(encoding="utf-8")
        for rel in ("pagekit/__init__.py", "pagekit/links.py")
    }
    files["tests/test_page_window.py"] = (
        "from pagekit import page_window\n\n\n"
        "def test_page_window_exists():\n"
        "    assert callable(page_window)\n"
    )
    result = submitter.regression_effectiveness(
        evaluation, _change_set(tmp_path, files), "U3"
    )
    assert result["baseline"]["result"] == "ALL_PASSED"
    assert result["classification"] == "INEFFECTIVE"


def test_bug_fix_cases_keep_the_unmodified_baseline_probe():
    for case_id in ("U1", "U2"):
        assert submitter.frozen_case(case_id).absent_symbol_stub == ()
