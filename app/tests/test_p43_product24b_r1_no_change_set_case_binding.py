"""P43-PRODUCT-24B-R1: the NO_CHANGE_SET model verdict carries case provenance.

U2 evidence: a legitimately failed model Task with no Change Set produced
``{"control": "model", "verdict": "NO_CHANGE_SET"}``.  The Block 2
classifier requires ``case_id``/``case_manifest_sha256`` on every non-empty
verdict, so the run was reported as CASE_BINDING_MISMATCH_HARNESS_FAILURE.
The verdict now copies the identity from the observed model record (the same
record the full verdict path cross-checks), so a genuine mismatch or a
missing identity still reaches the classifier as such.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.tests import test_p43_product23b_trusted_controls as c23b
from scripts.maintenance import p43_c2_trusted_submitter as submitter

LIVE_RUN = c23b.LIVE_RUN


def _run_no_change_set(
    tmp_path: Path, observation: dict
) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "root"
    (root / "env").mkdir(parents=True)
    (root / "u2-eval").mkdir()
    (root / "relay").mkdir()
    (root / "env" / "run.vars").write_text("CASE_ID=U2\n", encoding="utf-8")
    (root / "env" / "observe.model.json").write_text(
        json.dumps(observation), encoding="utf-8"
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
    return result, root / "eval" / "model" / "verdict.json"


def _observation(case_id: str | None, manifest: str | None) -> dict:
    observation = {
        "terminal": True,
        "task_execution_status": "failed",
        "change_set": None,
    }
    if case_id is not None:
        observation["case_id"] = case_id
    if manifest is not None:
        observation["case_manifest_sha256"] = manifest
    return observation


def test_failed_u2_task_without_change_set_keeps_u2_provenance(tmp_path):
    u2 = submitter.frozen_case("U2")
    result, verdict_path = _run_no_change_set(
        tmp_path, _observation("U2", u2.manifest_sha256)
    )

    assert result.returncode == 0, result.stderr
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict == {
        "control": "model",
        "verdict": "NO_CHANGE_SET",
        "case_id": "U2",
        "case_manifest_sha256": u2.manifest_sha256,
    }


def test_cross_case_observation_is_not_rewritten_to_the_selected_case(tmp_path):
    u1 = submitter.frozen_case("U1")
    _, verdict_path = _run_no_change_set(
        tmp_path, _observation("U1", u1.manifest_sha256)
    )

    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    # Selected case is U2; the U1 identity is carried through so the
    # classifier reports CASE_BINDING_MISMATCH_HARNESS_FAILURE.
    assert verdict["case_id"] == "U1"
    assert verdict["case_manifest_sha256"] == u1.manifest_sha256


def test_missing_observed_provenance_stays_missing(tmp_path):
    _, verdict_path = _run_no_change_set(tmp_path, _observation(None, None))

    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "NO_CHANGE_SET"
    assert verdict["case_id"] is None
    assert verdict["case_manifest_sha256"] is None
