"""Deterministic regressions for the canonical Celery startup gate."""

import subprocess
import sys
from pathlib import Path

from scripts.maintenance.wait_for_celery_worker import (
    ProbeResult,
    _celery_probe,
    wait_for_celery_worker,
)


EXPECTED = "celery@canonical-host"
HELPER = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "maintenance"
    / "wait_for_celery_worker.py"
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def _pong(node=EXPECTED):
    return ProbeResult(0, f"->  {node}: OK\n        pong\n")


def test_in_process_probe_uses_the_bounded_control_timeout():
    calls = []

    def ping(*, destination, timeout):
        calls.append((destination, timeout))
        return [{EXPECTED: {"ok": "pong"}}]

    result = _celery_probe(ping, EXPECTED, 0.8)

    assert result == _pong()
    assert calls == [([EXPECTED], 0.8)]


def test_direct_helper_invocation_can_import_the_canonical_celery_app(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--celery",
            sys.executable,
            "--pid",
            "999999999",
            "--expected-node",
            EXPECTED,
            "--timeout-seconds",
            "0.1",
            "--interval-seconds",
            "0.1",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1
    assert "worker process exited during control readiness" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


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
    probe_budgets = []

    def probe(remaining):
        probe_budgets.append(remaining)
        return ProbeResult(1, "Error: No nodes replied")

    result = wait_for_celery_worker(
        expected_node=EXPECTED,
        worker_pid=123,
        timeout_seconds=3,
        interval_seconds=1,
        probe=probe,
        process_alive=lambda pid: True,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result[0] is False
    assert "deadline expired" in result[1]
    assert "process remained alive but no Celery control response" in result[1]
    assert probe_budgets == [1, 1, 0.5]
    assert all(
        budget < remaining
        for budget, remaining in zip(probe_budgets, [3, 2, 1], strict=True)
    )
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
