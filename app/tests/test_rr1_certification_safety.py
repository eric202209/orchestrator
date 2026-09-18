"""RR1 deterministic regressions: certification safety and provider-free guard.

Covers RR1 sections 29 (registered-task safety assertion before delivery) and
30 (immediate halt with preserved evidence if a real provider call occurs).
"""

from __future__ import annotations

import pytest
from celery import Celery

from app.services.research.rr1.certification import (
    CertificationSafetyError,
    DEFAULT_CERTIFICATION_QUEUE,
    ProviderFreeGuard,
    assert_registered_task_set,
    build_certification_app,
    require_safe_certification_app,
)
from app.services.research.rr1.provider_capture import (
    CALLS_NONE,
    ProviderCallAccounting,
    ProviderCallRecord,
)

pytestmark = [pytest.mark.integration, pytest.mark.critical_regression]

SYNTHETIC_TASK = "rr1_certification.synthetic_lifecycle_probe"


def _certification_app(*, queue: str = DEFAULT_CERTIFICATION_QUEUE) -> Celery:
    """A Celery app built for certification: no production tasks registered."""

    app = build_certification_app(
        expected_tasks=[SYNTHETIC_TASK],
        broker_url="memory://",
        result_backend="cache+memory://",
        certification_queue=queue,
    )

    @app.task(name=SYNTHETIC_TASK)
    def _synthetic_lifecycle_probe(**_kwargs):  # pragma: no cover - never published
        return {"synthetic": True}

    return app


class TestRegisteredTaskSafetyAssertion:
    def test_clean_certification_app_is_safe(self):
        assertion = assert_registered_task_set(
            _certification_app(), expected_tasks=[SYNTHETIC_TASK]
        )
        assert assertion.safe is True
        assert assertion.findings == []
        assert assertion.observed == (SYNTHETIC_TASK,)
        assert assertion.production_tasks_present == ()

    def test_production_provider_capable_task_aborts_before_delivery(self):
        app = _certification_app()

        @app.task(name="app.tasks.worker.execute_orchestration_task")
        def _production(**_kwargs):  # pragma: no cover
            return None

        assertion = assert_registered_task_set(app, expected_tasks=[SYNTHETIC_TASK])
        assert assertion.safe is False
        assert (
            "app.tasks.worker.execute_orchestration_task"
            in assertion.production_tasks_present
        )
        with pytest.raises(CertificationSafetyError) as excinfo:
            require_safe_certification_app(app, expected_tasks=[SYNTHETIC_TASK])
        assert "production_provider_capable_tasks_registered" in str(excinfo.value)

    def test_any_production_task_module_is_rejected(self):
        app = _certification_app()

        @app.task(name="app.tasks.maintenance.sweep_stranded_continuation_deliveries")
        def _maintenance(**_kwargs):  # pragma: no cover
            return None

        assertion = assert_registered_task_set(app, expected_tasks=[SYNTHETIC_TASK])
        assert assertion.safe is False
        assert assertion.production_tasks_present

    def test_unexpected_extra_task_is_rejected(self):
        app = _certification_app()

        @app.task(name="rr1_certification.an_extra_task")
        def _extra(**_kwargs):  # pragma: no cover
            return None

        assertion = assert_registered_task_set(app, expected_tasks=[SYNTHETIC_TASK])
        assert assertion.safe is False
        assert "rr1_certification.an_extra_task" in assertion.unexpected

    def test_missing_expected_task_is_rejected(self):
        app = _certification_app()
        assertion = assert_registered_task_set(
            app, expected_tasks=[SYNTHETIC_TASK, "rr1_certification.absent"]
        )
        assert assertion.safe is False
        assert "rr1_certification.absent" in assertion.missing

    def test_routing_to_a_production_queue_is_rejected(self):
        app = _certification_app(queue="celery")
        assertion = assert_registered_task_set(app, expected_tasks=[SYNTHETIC_TASK])
        assert assertion.safe is False
        assert assertion.queue_violations
        assert any(
            "queue_routing_violations" in finding for finding in assertion.findings
        )

    def test_the_real_production_celery_app_is_never_safe_for_certification(self):
        """The BR2 root enabler: reusing the production app must be refused."""

        from app.celery_app import celery_app

        assertion = assert_registered_task_set(
            celery_app, expected_tasks=[SYNTHETIC_TASK]
        )
        assert assertion.safe is False
        assert (
            assertion.production_tasks_present
        ), "the production app registers provider-capable execution tasks"
        with pytest.raises(CertificationSafetyError):
            require_safe_certification_app(celery_app, expected_tasks=[SYNTHETIC_TASK])

    def test_a_naive_celery_app_inherits_production_tasks(self):
        """Why build_certification_app exists: a fresh Celery() is not clean.

        Celery keeps process-global app finalizers, so a new app picks up every
        task declared by an already-imported production module.  This is the
        BR2 root enabler, asserted here so a future change that quietly makes
        the naive path look safe is caught.
        """

        import app.tasks.worker  # noqa: F401  (ensure the module is imported)

        naive = Celery("rr1-naive", broker="memory://", backend="cache+memory://")
        naive.finalize()
        assertion = assert_registered_task_set(naive, expected_tasks=[SYNTHETIC_TASK])
        assert assertion.safe is False
        assert assertion.production_tasks_present

    def test_builder_purges_production_tasks_that_a_naive_app_would_inherit(self):
        import app.tasks.worker  # noqa: F401

        app_ = build_certification_app(
            expected_tasks=[SYNTHETIC_TASK],
            broker_url="memory://",
            result_backend="cache+memory://",
        )
        assert all(
            not name.startswith("app.tasks.") for name in app_.tasks
        ), "the certification registry must contain no production task"
        assert app_.conf.beat_schedule == {}

    def test_celery_builtin_tasks_are_not_treated_as_unexpected(self):
        assertion = assert_registered_task_set(
            _certification_app(), expected_tasks=[SYNTHETIC_TASK]
        )
        assert not any(name.startswith("celery.") for name in assertion.observed)


class TestProviderFreeGuard:
    def _record(self, **overrides) -> ProviderCallRecord:
        payload = {
            "call_id": "call-1",
            "correlation_id": "corr-1",
            "lane_id": "completion_repair",
            "declared_lane": None,
            "session_prefix": "completion-summary",
            "runtime_class": "synthetic.Runtime",
            "entry_method": "invoke_prompt",
            "nested": False,
        }
        payload.update(overrides)
        return ProviderCallRecord(**payload)

    def test_zero_calls_passes(self):
        accounting = ProviderCallAccounting(capture_installed=True)
        assert accounting.verdict == CALLS_NONE
        guard = ProviderFreeGuard(accounting=accounting)
        guard.check()
        assert guard.tripped is False

    def test_a_real_provider_call_halts_and_preserves_evidence(self):
        accounting = ProviderCallAccounting(capture_installed=True)
        accounting.records.append(self._record())
        guard = ProviderFreeGuard(accounting=accounting)

        with pytest.raises(CertificationSafetyError) as excinfo:
            guard.check()
        assert "real provider call occurred" in str(excinfo.value)
        assert guard.tripped is True
        # Evidence is preserved, never erased from accounting.
        assert guard.incident["call_count"] == 1
        assert guard.incident["by_lane"] == {"completion_repair": 1}
        assert guard.incident["records"][0]["call_id"] == "call-1"
        assert accounting.records, "the incident must remain in accounting"

    def test_incomplete_capture_halts_rather_than_reporting_zero(self):
        accounting = ProviderCallAccounting(capture_installed=True)
        accounting.record_gap("an_uninstrumented_boundary")
        guard = ProviderFreeGuard(accounting=accounting)
        with pytest.raises(CertificationSafetyError) as excinfo:
            guard.check()
        assert "incomplete" in str(excinfo.value)
        assert guard.incident["capture_gaps"] == ["an_uninstrumented_boundary"]

    def test_guard_does_not_depend_on_an_expected_call(self):
        """Detection must be by observation, not by intent."""

        accounting = ProviderCallAccounting(capture_installed=True)
        accounting.records.append(self._record(declared_lane=None))
        guard = ProviderFreeGuard(accounting=accounting)
        with pytest.raises(CertificationSafetyError):
            guard.check()
        assert accounting.unexpected_calls
