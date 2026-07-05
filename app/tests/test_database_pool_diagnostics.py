"""Regression tests for the DB connection-pool diagnostic helper added in
Phase 19A (see docs/roadmap/done/phase19/phase19a-db-pool-sqlite-concurrency-hardening-report.md).

Pool exhaustion (Phase 18L-R) was only discovered by reading a
sqlalchemy.exc.TimeoutError traceback out of the backend log after the API
was already wedged. get_pool_status() and its exposure through /health give
an operator a way to see the pool approaching exhaustion beforehand.
"""

from __future__ import annotations

from app.database import get_pool_status
from app.services.observability.health import health_payload


def test_get_pool_status_reports_expected_keys():
    status = get_pool_status()

    assert set(status.keys()) == {"size", "checked_out", "overflow", "checked_in"}


def test_health_payload_includes_database_pool_diagnostics():
    payload, _status_code = health_payload()

    assert "database_pool" in payload["details"]
    pool_status = payload["details"]["database_pool"]
    assert set(pool_status.keys()) == {"size", "checked_out", "overflow", "checked_in"}
