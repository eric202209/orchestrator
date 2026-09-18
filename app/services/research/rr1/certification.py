"""Provider-free infrastructure certification safety (§28, §29).

The BR2 incident showed that an intended provider-free certification can
accidentally invoke a real provider: three unintended provider calls were made
before the corrected certification reached zero.  The root enabler was reusing
a Celery application that had production provider-capable execution tasks
registered and routable.

This module therefore refuses to publish synthetic certification work until it
has asserted, against the live application object, that:

* the registered task set is exactly the expected synthetic set;
* no production provider-capable task name is registered;
* every synthetic task routes to the dedicated certification queue, and no
  synthetic task routes to a production queue.

The assertion runs *before* delivery, and a mismatch aborts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: Production task names that must never be registered on a certification app.
#: ``execute_orchestration_task`` is the provider-capable execution entry the
#: BR2 incident actually reached.
PRODUCTION_PROVIDER_CAPABLE_TASKS: tuple[str, ...] = (
    "app.tasks.worker.execute_orchestration_task",
)

#: Whole modules whose tasks are provider-capable or lifecycle-mutating.
PRODUCTION_TASK_MODULE_PREFIXES: tuple[str, ...] = (
    "app.tasks.worker.",
    "app.tasks.maintenance.",
    "app.tasks.planning_tasks.",
    "app.tasks.github_tasks.",
)

DEFAULT_CERTIFICATION_QUEUE = "rr1-certification"


def build_certification_app(
    *,
    expected_tasks: Iterable[str],
    broker_url: str,
    result_backend: str,
    certification_queue: str = DEFAULT_CERTIFICATION_QUEUE,
    name: str = "rr1-certification",
) -> Any:
    """Build a Celery app whose registry is exactly the expected synthetic set.

    Constructing a fresh ``Celery()`` is *not* sufficient isolation.  Celery
    keeps a process-global set of app finalizers, so every task declared by an
    already-imported production module is added to any new app when it
    finalizes.  In a process that has imported ``app.tasks.worker`` -- which a
    certification run generally has, because it imports the models and
    services those tasks share -- a naive certification app therefore has
    ``execute_orchestration_task`` registered and routable.  That is the exact
    condition that let the BR2 certification reach a real provider.

    This builder finalizes the app and then unregisters every task that is not
    expected, so the subsequent assertion verifies a registry that is exactly
    the synthetic set rather than merely hoping for one.
    """

    from celery import Celery

    expected = tuple(sorted(set(expected_tasks)))
    app = Celery(name, broker=broker_url, backend=result_backend)
    app.conf.update(
        task_default_queue=certification_queue,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,
        # Beat schedules belong to production; a certification app runs none.
        beat_schedule={},
    )
    app.finalize()
    for name_ in list(app.tasks):
        if name_.startswith("celery.") or name_ in expected:
            continue
        app.tasks.unregister(name_)
    return app


class CertificationSafetyError(RuntimeError):
    """Raised before delivery when the certification app is unsafe."""


@dataclass
class RegisteredTaskAssertion:
    safe: bool
    expected: tuple[str, ...]
    observed: tuple[str, ...]
    unexpected: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    production_tasks_present: tuple[str, ...] = ()
    queue_violations: tuple[str, ...] = ()
    broker_url: str | None = None
    findings: list[str] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "expected_tasks": list(self.expected),
            "observed_tasks": list(self.observed),
            "unexpected_tasks": list(self.unexpected),
            "missing_tasks": list(self.missing),
            "production_tasks_present": list(self.production_tasks_present),
            "queue_violations": list(self.queue_violations),
            "broker_url": self.broker_url,
            "findings": list(self.findings),
        }


def _visible_tasks(celery_app: Any) -> tuple[str, ...]:
    """Registered task names excluding Celery's own built-ins."""

    names = list(getattr(celery_app, "tasks", {}) or {})
    return tuple(sorted(name for name in names if not name.startswith("celery.")))


def assert_registered_task_set(
    celery_app: Any,
    *,
    expected_tasks: Iterable[str],
    certification_queue: str = DEFAULT_CERTIFICATION_QUEUE,
) -> RegisteredTaskAssertion:
    """Assert the exact registered task set and queue routing before delivery."""

    expected = tuple(sorted(set(expected_tasks)))
    observed = _visible_tasks(celery_app)

    unexpected = tuple(name for name in observed if name not in expected)
    missing = tuple(name for name in expected if name not in observed)
    production_present = tuple(
        name
        for name in observed
        if name in PRODUCTION_PROVIDER_CAPABLE_TASKS
        or any(name.startswith(prefix) for prefix in PRODUCTION_TASK_MODULE_PREFIXES)
    )

    conf = getattr(celery_app, "conf", None)
    default_queue = getattr(conf, "task_default_queue", None)
    routes = getattr(conf, "task_routes", None) or {}
    queue_violations: list[str] = []
    for name in expected:
        route = routes.get(name) if isinstance(routes, dict) else None
        queue = (route or {}).get("queue") if isinstance(route, dict) else None
        effective = queue or default_queue
        if effective != certification_queue:
            queue_violations.append(f"{name}->{effective!r}")

    findings: list[str] = []
    if unexpected:
        findings.append(f"unexpected_registered_tasks:{','.join(unexpected)}")
    if missing:
        findings.append(f"missing_expected_tasks:{','.join(missing)}")
    if production_present:
        findings.append(
            f"production_provider_capable_tasks_registered:{','.join(production_present)}"
        )
    if queue_violations:
        findings.append(f"queue_routing_violations:{','.join(queue_violations)}")

    return RegisteredTaskAssertion(
        safe=not findings,
        expected=expected,
        observed=observed,
        unexpected=unexpected,
        missing=missing,
        production_tasks_present=production_present,
        queue_violations=tuple(queue_violations),
        broker_url=str(getattr(conf, "broker_url", None) or "") or None,
        findings=findings,
    )


def require_safe_certification_app(
    celery_app: Any,
    *,
    expected_tasks: Iterable[str],
    certification_queue: str = DEFAULT_CERTIFICATION_QUEUE,
) -> RegisteredTaskAssertion:
    """Abort before publishing synthetic work when the app is unsafe."""

    assertion = assert_registered_task_set(
        celery_app,
        expected_tasks=expected_tasks,
        certification_queue=certification_queue,
    )
    if not assertion.safe:
        raise CertificationSafetyError(
            "RR1 certification aborted before delivery: "
            + "; ".join(assertion.findings)
        )
    return assertion


@dataclass
class ProviderFreeGuard:
    """Stop a certification immediately on the first real provider call (§30)."""

    accounting: Any
    tripped: bool = False
    incident: dict[str, Any] | None = None

    def check(self) -> None:
        from app.services.research.rr1.provider_capture import (
            CALLS_NONE,
            CALLS_UNKNOWN,
        )

        verdict = self.accounting.verdict
        if verdict == CALLS_NONE:
            return
        self.tripped = True
        self.incident = {
            "verdict": verdict,
            "call_count": self.accounting.call_count,
            "by_lane": self.accounting.by_lane(),
            "capture_gaps": list(self.accounting.capture_gaps),
            # Evidence is preserved, never erased from accounting.
            "records": [record.as_evidence() for record in self.accounting.records],
        }
        if verdict == CALLS_UNKNOWN:
            raise CertificationSafetyError(
                "RR1 certification halted: provider-call capture is incomplete; "
                "an incomplete capture is never reported as zero"
            )
        raise CertificationSafetyError(
            "RR1 certification halted: a real provider call occurred during a "
            f"provider-free certification (verdict={verdict}, "
            f"count={self.accounting.call_count})"
        )
