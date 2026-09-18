"""RR1 deterministic regressions: configuration, attribution, manifest, drift.

Covers RR1 sections 16 (completion-repair configuration capture),
17 (ProductRoot/run attribution), 19 (treatment manifest) and 20/27 (drift
detection with MATCH / DRIFT / UNVERIFIABLE).
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services.research.rr1.attribution import (
    INTEGRITY_OK,
    INTEGRITY_UNATTRIBUTED_PROVIDER_CALL,
    INTEGRITY_UNATTRIBUTED_WORKSPACE_MUTATION,
    RunAttribution,
    git_identity,
)
from app.services.research.rr1.completion_repair_config import (
    ENABLE_ENV_VAR,
    UNRESOLVED,
    capture_completion_repair_configuration,
)
from app.services.research.rr1.manifest import (
    DRIFT,
    MANIFEST_SCHEMA_VERSION,
    MATCH,
    TREATMENT_FIELDS,
    UNREADABLE,
    UNVERIFIABLE,
    capture_treatment_manifest,
    compare_to_manifest,
    write_manifest,
)

pytestmark = [pytest.mark.integration, pytest.mark.critical_regression]

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Section 16 -- completion-repair configuration
# ---------------------------------------------------------------------------


class TestCompletionRepairConfiguration:
    def test_every_required_configuration_dimension_is_observable(self, monkeypatch):
        monkeypatch.setenv(ENABLE_ENV_VAR, "1")
        captured = capture_completion_repair_configuration()
        configured = captured.configured

        for field in (
            "enabled",
            "timeout_seconds",
            "retry_count",
            "no_output_timeout_seconds",
            "temperature",
            "max_output_tokens",
            "thinking_configuration",
            "trigger_conditions",
            "declared_role",
        ):
            assert field in configured, field

        assert configured["enabled"] is True
        assert configured["declared_role"] == "repair"
        assert configured["session_prefix"] == "completion-summary"
        assert configured["timeout_seconds"] == 45.0
        assert configured["max_output_tokens"] == 512
        assert configured["temperature"] == 0.0
        assert configured["reasoning_enabled"] is False
        assert configured["retry_count"] == 0
        assert configured["no_output_timeout_seconds"] is None

    def test_disabled_flag_is_observed_not_assumed(self, monkeypatch):
        monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
        captured = capture_completion_repair_configuration()
        assert captured.configured["enabled"] is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_truthy_enable_values(self, monkeypatch, value):
        monkeypatch.setenv(ENABLE_ENV_VAR, value)
        assert capture_completion_repair_configuration().configured["enabled"] is True

    def test_resolved_values_are_unresolved_without_a_database(self):
        captured = capture_completion_repair_configuration(db=None)
        assert captured.resolved["status"] == UNRESOLVED
        assert captured.resolved["reason"] == "no_database_context_supplied"

    def test_resolved_values_are_captured_with_a_database(self, db_session):
        captured = capture_completion_repair_configuration(db=db_session)
        assert captured.resolved["status"] in {"RESOLVED", UNRESOLVED}
        if captured.resolved["status"] == "RESOLVED":
            assert captured.resolved["role"] == "repair"
            assert captured.resolved["backend"]
            assert captured.resolved["model_family"]

    def test_evidence_source_names_where_each_value_came_from(self):
        captured = capture_completion_repair_configuration()
        source = captured.evidence_source
        assert "completion_summary.py" in source["enabled"]
        assert "SUMMARY_TIMEOUT_SECONDS" in source["timeout_seconds"]
        assert "BackendRole.REPAIR" in source["role"]

    def test_role_mismatch_between_declared_and_available_role_is_recorded(self):
        captured = capture_completion_repair_configuration()
        assert any(
            "COMPLETION_REPAIR" in note for note in captured.notes
        ), "the declared/available role difference must be observable"


# ---------------------------------------------------------------------------
# Section 17 -- attribution
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "rr1@test.invalid"],
        ["config", "user.name", "RR1"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, env={**env})
    (path / "file.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env={**env})
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "baseline"],
        check=True,
        env={
            **env,
            "GIT_AUTHOR_NAME": "RR1",
            "GIT_AUTHOR_EMAIL": "rr1@test.invalid",
            "GIT_COMMITTER_NAME": "RR1",
            "GIT_COMMITTER_EMAIL": "rr1@test.invalid",
        },
    )


class TestRunAttribution:
    def test_attribution_schema_binds_run_to_productroot_and_workspace(self, tmp_path):
        root = tmp_path / "productroot"
        _git_repo(root)
        attribution = RunAttribution(
            research_run_id="rr1-run-1",
            correlation_id="corr-1",
            project_id=1,
            session_id=2,
            task_id=3,
            task_execution_ids=[4],
            productroot_path=str(root),
            workspace_path=str(tmp_path / "workspace"),
        )
        attribution.open_baseline()
        attribution.close_final()
        evidence = attribution.as_evidence()

        for field in (
            "research_run_id",
            "task_id",
            "project_id",
            "session_id",
            "task_execution_ids",
            "productroot_path",
            "productroot_baseline",
            "productroot_final",
            "workspace_path",
            "provider_call_ids",
            "lifecycle_evidence",
            "physical_evidence",
        ):
            assert field in evidence, field
        assert evidence["productroot_baseline"]["head"]
        assert evidence["productroot_baseline"]["tree"]
        assert evidence["productroot_mutated"] is False
        assert evidence["integrity_status"] == INTEGRITY_OK

    def test_productroot_mutation_is_detected(self, tmp_path):
        root = tmp_path / "productroot"
        _git_repo(root)
        attribution = RunAttribution(
            research_run_id="rr1-run-1",
            correlation_id="corr-1",
            productroot_path=str(root),
        )
        attribution.open_baseline()
        (root / "file.txt").write_text("mutated\n", encoding="utf-8")
        attribution.close_final()
        assert attribution.productroot_mutated is True

    def test_unreadable_productroot_is_an_integrity_finding_not_a_clean_zero(
        self, tmp_path
    ):
        attribution = RunAttribution(
            research_run_id="rr1-run-1",
            correlation_id="corr-1",
            productroot_path=str(tmp_path / "does-not-exist"),
        )
        attribution.open_baseline()
        attribution.close_final()
        assert attribution.productroot_mutated is None
        assert attribution.integrity_status != INTEGRITY_OK

    def test_provider_call_with_foreign_correlation_is_an_integrity_problem(self):
        class _Record:
            nested = False
            call_id = "call-1"
            correlation_id = "some-other-run"

        class _Accounting:
            records = [_Record()]

        attribution = RunAttribution(
            research_run_id="rr1-run-1", correlation_id="corr-1"
        )
        attribution.attribute_provider_calls(_Accounting())
        assert any(
            finding.startswith(INTEGRITY_UNATTRIBUTED_PROVIDER_CALL)
            for finding in attribution.integrity_findings
        )

    def test_unattributed_workspace_mutation_is_recorded(self):
        attribution = RunAttribution(
            research_run_id="rr1-run-1", correlation_id="corr-1"
        )
        attribution.note_workspace_mutation("/some/path", attributed=False)
        assert any(
            finding.startswith(INTEGRITY_UNATTRIBUTED_WORKSPACE_MUTATION)
            for finding in attribution.integrity_findings
        )

    def test_git_identity_on_a_non_repo_reports_an_error(self, tmp_path):
        identity = git_identity(tmp_path)
        assert identity.error is not None
        assert identity.head is None


# ---------------------------------------------------------------------------
# Section 19 -- treatment manifest
# ---------------------------------------------------------------------------


class TestTreatmentManifest:
    def test_manifest_freezes_every_required_field(self, db_session):
        manifest = capture_treatment_manifest(
            repo_root=REPO_ROOT, db=db_session, label="rr1-template"
        )
        assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
        for field in TREATMENT_FIELDS:
            assert field in manifest, field
        assert manifest["orchestrator_head"] != UNREADABLE
        assert manifest["orchestrator_tree"] != UNREADABLE
        assert manifest["harness_version"].startswith("rr1/")
        assert manifest["lifecycle_authority_hash"] != UNREADABLE
        assert manifest["br2_reconciliation_hash"] != UNREADABLE
        assert manifest["completion_repair_configuration"]["configured"]

    def test_manifest_is_not_an_authorized_cohort_freeze(self, db_session):
        manifest = capture_treatment_manifest(repo_root=REPO_ROOT, db=db_session)
        assert manifest["frozen_as_authorized_cohort"] is False

    def test_missing_oracle_file_is_recorded_as_unreadable(self, tmp_path, db_session):
        manifest = capture_treatment_manifest(
            repo_root=REPO_ROOT,
            db=db_session,
            oracle_paths=(tmp_path / "missing-oracle.py",),
        )
        assert set(manifest["oracle_hashes"].values()) == {UNREADABLE}

    def test_manifest_round_trips_to_disk(self, tmp_path, db_session):
        manifest = capture_treatment_manifest(repo_root=REPO_ROOT, db=db_session)
        target = write_manifest(tmp_path / "manifest.json", manifest)
        assert json.loads(target.read_text(encoding="utf-8")) == json.loads(
            json.dumps(manifest, sort_keys=True, default=str)
        )
        mode = target.stat().st_mode & 0o777
        assert mode & 0o060, "manifest must stay group-writable in a shared workspace"


# ---------------------------------------------------------------------------
# Sections 20 / 27 -- drift detection
# ---------------------------------------------------------------------------


class TestDriftDetector:
    @pytest.fixture
    def frozen(self, db_session):
        return capture_treatment_manifest(repo_root=REPO_ROOT, db=db_session)

    def test_unchanged_state_is_match_and_permits_launch(self, frozen):
        report = compare_to_manifest(frozen, copy.deepcopy(frozen))
        assert report.verdict == MATCH
        assert report.launch_permitted is True
        assert report.drifted_fields == ()
        assert report.unverifiable_fields == ()

    def test_unconfigured_optional_completion_role_is_readable(
        self, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", None)
        monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "")

        manifest = capture_treatment_manifest(repo_root=REPO_ROOT, db=db_session)
        role = manifest["provider_role_resolution"]["completion_repair"]

        assert role["status"] == "UNCONFIGURED"
        report = compare_to_manifest(manifest, copy.deepcopy(manifest))
        assert report.verdict == MATCH
        assert report.unverifiable_fields == ()

    @pytest.mark.parametrize(
        "field,mutate",
        [
            ("pc1_hash", lambda v: "0" * 64),
            ("sb1_values", lambda v: {**v, "SB1_BUDGET": "changed"}),
            ("provider_role_resolution", lambda v: {"planning": {"model": "other"}}),
            ("timeouts", lambda v: {**v, "RR1_SYNTHETIC_TIMEOUT_SECONDS": 999}),
            ("completion_repair_configuration", lambda v: {**v, "configured": {}}),
            ("lifecycle_authority_hash", lambda v: "1" * 64),
            ("harness_version", lambda v: "rr1/9.9.9"),
            ("oracle_hashes", lambda v: {"oracle.py": "2" * 64}),
            ("grounding_budgets", lambda v: {"GROUNDING_MAX_TURNS": 99}),
            ("retry_limits", lambda v: {"MAX_TASK_RETRIES": 99}),
        ],
    )
    def test_each_treatment_relevant_change_produces_drift_and_blocks_launch(
        self, frozen, field, mutate
    ):
        observed = copy.deepcopy(frozen)
        observed[field] = mutate(observed[field])
        report = compare_to_manifest(frozen, observed)
        assert report.verdict == DRIFT
        assert field in report.drifted_fields
        assert report.launch_permitted is False

    def test_unreadable_observation_is_unverifiable_not_match(self, frozen):
        observed = copy.deepcopy(frozen)
        observed["pc1_hash"] = UNREADABLE
        report = compare_to_manifest(frozen, observed)
        assert report.verdict == UNVERIFIABLE
        assert "pc1_hash" in report.unverifiable_fields
        assert report.launch_permitted is False

    def test_unreadable_nested_value_is_unverifiable(self, frozen):
        observed = copy.deepcopy(frozen)
        observed["provider_routing"] = {"OPENAI_BASE_URL": UNREADABLE}
        report = compare_to_manifest(frozen, observed)
        assert report.verdict == UNVERIFIABLE
        assert "provider_routing" in report.unverifiable_fields

    def test_absent_field_is_unverifiable_not_match(self, frozen):
        observed = copy.deepcopy(frozen)
        observed.pop("sb1_values")
        report = compare_to_manifest(frozen, observed)
        assert report.verdict == UNVERIFIABLE
        assert "sb1_values" in report.unverifiable_fields

    def test_drift_outranks_unverifiable_in_the_overall_verdict(self, frozen):
        observed = copy.deepcopy(frozen)
        observed["pc1_hash"] = "0" * 64
        observed.pop("sb1_values")
        report = compare_to_manifest(frozen, observed)
        assert report.verdict == DRIFT
        assert report.launch_permitted is False

    def test_detector_never_auto_updates_the_frozen_manifest(self, frozen):
        before = copy.deepcopy(frozen)
        observed = copy.deepcopy(frozen)
        observed["pc1_hash"] = "0" * 64
        compare_to_manifest(frozen, observed)
        assert frozen == before, "the frozen manifest must never be auto-updated"
