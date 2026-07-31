"""Deterministic regressions for the canonical Celery startup gate."""

from scripts.maintenance.wait_for_celery_worker import (
    ProbeResult,
    wait_for_celery_worker,
)


EXPECTED = "celery@canonical-host"


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def _pong(node=EXPECTED):
    return ProbeResult(0, f"->  {node}: OK\n        pong\n")


def test_delayed_control_readiness_retries_then_succeeds():
    clock = Clock()
    probes = iter([ProbeResult(1, "Error: No nodes replied"), _pong()])
    probe_budgets = []

    def probe(remaining):
        probe_budgets.append(remaining)
        return next(probes)

    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=5,
        interval_seconds=1,
        probe=probe,
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is True
    assert "attempt 2" in result[1]
    assert probe_budgets == [1, 1]
    assert clock.now == 1


def test_permanent_control_unavailability_fails_at_bounded_deadline():
    clock = Clock()
    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=3,
        interval_seconds=1,
        probe=lambda remaining: ProbeResult(1, "Error: No nodes replied"),
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is False
    assert "deadline expired" in result[1]
    assert "process remained alive but no Celery control response" in result[1]
    assert clock.now == 3


def test_worker_exit_stops_polling_without_waiting_for_deadline():
    clock = Clock()
    alive = iter([True, False])
    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=30,
        interval_seconds=1,
        probe=lambda remaining: ProbeResult(1, "Error: No nodes replied"),
        process_alive=lambda pid: next(alive),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result == (False, "worker process exited during control readiness (pid 123)")
    assert clock.now == 0


def test_unexpected_responding_node_is_not_accepted():
    clock = Clock()
    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=1,
        interval_seconds=1,
        probe=lambda remaining: _pong("celery@stale-host"),
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is False
    assert "unexpected responding node(s) celery@stale-host" in result[1]


def test_immediate_expected_response_does_not_sleep():
    clock = Clock()
    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=5,
        interval_seconds=1,
        probe=lambda remaining: _pong(),
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is True
    assert clock.now == 0


def test_broker_failure_is_reported_without_becoming_ready():
    clock = Clock()
    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=1,
        interval_seconds=1,
        probe=lambda remaining: ProbeResult(
            1, "Error: Connection refused to Redis broker"
        ),
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is False
    assert "broker/control transport unavailable" in result[1]
