"""P43-PRODUCT-24B: classified regression effectiveness (provider-free).

The frozen P02 oracle counts any non-passing baseline outcome as effective.
The independent model verdict must additionally require an attributable
assertion failure on the complete baseline and PASS on the candidate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tests import test_p43_product24a_u2_case_adaptation as c24a
from scripts.maintenance import p43_c2_trusted_submitter as submitter


SOURCE = """def parse(text):
    return int(text.strip("()"))
"""
FIXED = """def parse(text):
    if text.startswith("("):
        return -int(text[1:-1])
    return int(text)
"""
EXISTING = """from pkg import parse


def test_plain():
    assert parse("7") == 7
"""


@pytest.fixture
def synthetic_case(tmp_path, monkeypatch):
    """A tiny frozen-case layout; material identity is not under test here."""

    root = tmp_path / "eval"
    baseline = root / submitter.frozen_case("U2").directory / "baseline"
    (baseline / "pkg").mkdir(parents=True)
    (baseline / "tests").mkdir()
    (baseline / "pkg" / "__init__.py").write_text("from .core import parse\n")
    (baseline / "pkg" / "core.py").write_text(SOURCE)
    (baseline / "tests" / "test_existing.py").write_text(EXISTING)
    (baseline / "tests" / "test_acceptance.py").write_text(EXISTING)
    manifest = {
        "acceptance": "tests/test_acceptance.py",
        "existing_tests": ["tests/test_existing.py"],
    }
    monkeypatch.setattr(submitter, "verify_case_material", lambda *a: manifest)
    return root


def _change_set(tmp_path: Path, files: dict[str, str], deleted=()) -> Path:
    cs = tmp_path / f"cs-{len(list(tmp_path.glob('cs-*')))}"
    for rel, text in files.items():
        (cs / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
        (cs / "files" / rel).write_text(text)
    (cs / "manifest.json").write_text(
        json.dumps({"copied_files": sorted(files), "deleted_files": list(deleted)})
    )
    return cs


def _classify(root: Path, cs: Path) -> dict:
    return submitter.regression_effectiveness(root, cs, "U2")


def test_meaningful_baseline_assertion_failure_and_candidate_pass(
    synthetic_case, tmp_path
):
    test = "from pkg import parse\n\n\ndef test_neg():\n    assert parse('(7)') == -7\n"
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": FIXED, "tests/test_neg.py": test}),
    )
    assert result["classification"] == submitter.REGRESSION_EFFECTIVE
    assert result["baseline"]["result"] == "ASSERTION_FAILURE"
    assert result["baseline"]["cases"] == {"test_neg": "assertion_failure"}
    assert result["candidate"]["result"] == "PASS"
    assert result["workspaces"] == "complete_baseline_and_complete_candidate"


def test_did_not_raise_counts_as_assertion_failure(synthetic_case, tmp_path):
    fixed = FIXED.replace(
        '    if text.startswith("("):',
        '    if text.startswith("(") and not text.endswith(")"):\n'
        "        raise ValueError(text)\n"
        '    if text.startswith("("):',
    )
    test = (
        "import pytest\nfrom pkg import parse\n\n\ndef test_unbalanced():\n"
        "    with pytest.raises(ValueError):\n        parse('(7')\n"
    )
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": fixed, "tests/test_u.py": test}),
    )
    assert result["classification"] == submitter.REGRESSION_EFFECTIVE


def test_module_level_import_failure_is_not_effective(synthetic_case, tmp_path):
    # The frozen oracle reports this as COMPLETE: the new test is "absent" on
    # the baseline because collection is interrupted.
    fixed = FIXED + "\n\ndef parse_signed(text):\n    return parse(text)\n"
    test = (
        "from pkg.core import parse_signed\n\n\ndef test_signed():\n"
        "    assert parse_signed('(7)') == -7\n"
    )
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": fixed, "tests/test_s.py": test}),
    )
    assert result["baseline"]["result"] == "COLLECTION_OR_IMPORT_FAILURE"
    assert result["candidate"]["result"] == "PASS"
    assert result["classification"] == "BASELINE_COLLECTION_OR_IMPORT_FAILURE"


def test_import_failure_inside_test_is_not_effective(synthetic_case, tmp_path):
    fixed = FIXED + "\n\ndef parse_signed(text):\n    return parse(text)\n"
    test = (
        "def test_signed():\n    from pkg.core import parse_signed\n"
        "    assert parse_signed('(7)') == -7\n"
    )
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": fixed, "tests/test_s.py": test}),
    )
    assert result["baseline"]["cases"] == {"test_signed": "import_failure"}
    assert result["classification"] == "BASELINE_IMPORT_FAILURE"


def test_skipped_and_all_passing_tests_are_not_effective(synthetic_case, tmp_path):
    skipped = "import pytest\n\n\ndef test_later():\n    pytest.skip('todo')\n"
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": FIXED, "tests/test_k.py": skipped}),
    )
    assert result["classification"] == "BASELINE_NO_ASSERTION_FAILURE"
    passing = (
        "from pkg import parse\n\n\ndef test_plain2():\n    assert parse('8') == 8\n"
    )
    result = _classify(
        synthetic_case,
        _change_set(tmp_path, {"pkg/core.py": FIXED, "tests/test_p.py": passing}),
    )
    assert result["classification"] == "INEFFECTIVE"


def test_candidate_failure_missing_tests_and_unparsable_tests(synthetic_case, tmp_path):
    test = "from pkg import parse\n\n\ndef test_neg():\n    assert parse('(7)') == -7\n"
    result = _classify(
        synthetic_case, _change_set(tmp_path, {"tests/test_neg.py": test})
    )
    assert result["baseline"]["result"] == "ASSERTION_FAILURE"
    assert result["classification"] == "CANDIDATE_FAILED"
    result = _classify(synthetic_case, _change_set(tmp_path, {"pkg/core.py": FIXED}))
    assert result["classification"] == "NO_REGRESSION_TESTS"
    result = _classify(
        synthetic_case,
        _change_set(
            tmp_path, {"pkg/core.py": FIXED, "tests/test_bad.py": "def test_(:\n"}
        ),
    )
    assert result["classification"] == "COLLECTION_OR_IMPORT_FAILURE"


def test_timeout_and_infrastructure_failure_are_distinct(
    synthetic_case, tmp_path, monkeypatch
):
    test = "from pkg import parse\n\n\ndef test_neg():\n    assert parse('(7)') == -7\n"
    cs = _change_set(tmp_path, {"pkg/core.py": FIXED, "tests/test_neg.py": test})

    def timeout(*args, **kwargs):
        raise submitter.subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(submitter.subprocess, "run", timeout)
    assert _classify(synthetic_case, cs)["classification"] == "BASELINE_TIMEOUT"

    def missing(*args, **kwargs):
        raise FileNotFoundError("python")

    monkeypatch.setattr(submitter.subprocess, "run", missing)
    assert (
        _classify(synthetic_case, cs)["classification"]
        == "BASELINE_INFRASTRUCTURE_FAILURE"
    )


def test_unsafe_change_set_paths_are_refused(synthetic_case, tmp_path):
    cs = tmp_path / "cs-unsafe"
    cs.mkdir()
    (cs / "manifest.json").write_text(json.dumps({"copied_files": ["../escape.py"]}))
    with pytest.raises(submitter.SubmitterRefused, match="unsafe_change_set_path"):
        _classify(synthetic_case, cs)


def _model_verdict(monkeypatch, regression):
    monkeypatch.setattr(
        submitter,
        "verify_case_material",
        lambda *a: {"baseline_files": {}, "acceptance": "a", "existing_tests": []},
    )
    monkeypatch.setattr(
        submitter, "change_set_integrity", lambda *a: {"ok": True, "reasons": []}
    )
    monkeypatch.setattr(
        submitter.Path, "read_text", lambda self, *a, **k: '{"expectations": {}}'
    )
    case = submitter.frozen_case("U2")
    observed = {
        "control": submitter.MODEL,
        "case_id": case.id,
        "case_manifest_sha256": case.manifest_sha256,
        "change_set": {},
        "workspace_files": {},
        "expected_outcome_met": True,
    }
    return submitter.evaluation_verdict(
        submitter.MODEL,
        "/nonexistent",
        "/nonexistent",
        {"complete_delivery": True},
        {"hidden_pass": True},
        observed,
        "U2",
        regression,
    )


@pytest.mark.parametrize(
    ("regression", "verdict"),
    [
        ({"classification": "EFFECTIVE"}, "COMPLETE_DELIVERY"),
        (
            {"classification": "BASELINE_COLLECTION_OR_IMPORT_FAILURE"},
            "INCOMPLETE_DELIVERY",
        ),
        ({"classification": "EVALUATOR_FAILURE"}, "INCOMPLETE_DELIVERY"),
        (None, "INCOMPLETE_DELIVERY"),
    ],
)
def test_model_complete_delivery_requires_effective_regression(
    monkeypatch, regression, verdict
):
    result = _model_verdict(monkeypatch, regression)
    assert result["verdict"] == verdict
    assert result["observed"]["complete_delivery_with_hidden"] is (
        verdict == "COMPLETE_DELIVERY"
    )
    assert result["observed"]["regression_effectiveness"] == (
        (regression or {}).get("classification") or "absent"
    )


def test_unreadable_regression_evidence_is_recorded_not_passed(tmp_path):
    assert submitter._load_regression(None) is None
    assert submitter._load_regression(str(tmp_path / "absent.json")) == {
        "classification": "EVALUATOR_FAILURE"
    }
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert submitter._load_regression(str(empty)) == {
        "classification": "EVALUATOR_FAILURE"
    }


def test_runner_runs_regression_in_eval_container_and_preserves_it():
    runner = c24a.LIVE_RUN.read_text(encoding="utf-8")
    assert '-v "$SUBMITTER:/tools/p43_c2_trusted_submitter.py:ro"' in runner
    assert (
        "/tools/p43_c2_trusted_submitter.py regression /eval /cs" in runner
        and '>"$out/regression.json" || true' in runner
    )
    assert '--regression "$out/regression.json"' in runner
    assert "for f in oracle hidden regression verdict contamination; do" in runner


@pytest.mark.parametrize(
    ("control", "classification"),
    [
        ("reference", "EFFECTIVE"),
        ("regressing", "EFFECTIVE"),
        ("ineffective-test", "INEFFECTIVE"),
        ("missing-test", "NO_REGRESSION_TESTS"),
    ],
)
def test_frozen_u2_controls_calibrate(tmp_path, control, classification):
    material = c24a._stage(tmp_path, "evaluation", "U2")
    controls = c24a.EVIDENCE / submitter.U2_DIR / "controls"
    result = submitter.regression_effectiveness(material, controls / control, "U2")
    assert result["classification"] == classification
