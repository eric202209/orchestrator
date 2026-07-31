#!/usr/bin/env python3
"""Bounded, fail-closed Celery control-plane readiness probe for start.sh."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_INTERVAL_SECONDS = 1.0
MAX_PROBE_SECONDS = 1.0
PROBE_TEARDOWN_MARGIN_SECONDS = 0.1
_RESPONDING_NODE = re.compile(r"^\s*->\s+(?P<node>\S+):\s+OK\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ProbeResult:
    """One Celery control probe result, retained for timeout diagnostics."""

    returncode: int
    output: str


def _summarize_output(output: str) -> str:
    normalized = " ".join(output.split())
    return normalized[:500] if normalized else "no command output"


def _responding_nodes(output: str) -> set[str]:
    return set(_RESPONDING_NODE.findall(output))


def _timeout_diagnostic(expected_node: str, result: ProbeResult) -> str:
    output = _summarize_output(result.output)
    normalized = output.lower()
    if "no nodes replied" in normalized or "no reply" in normalized:
        return (
            "control readiness deadline expired: worker process remained alive "
            f"but no Celery control response arrived from expected worker {expected_node}; "
            f"last probe exit {result.returncode}: {output}"
        )
    if any(
        marker in normalized for marker in ("broker", "redis", "connection refused")
    ):
        return (
            "control readiness deadline expired: Celery broker/control transport unavailable "
            f"for {expected_node}; last probe exit {result.returncode}: {output}"
        )
    return (
        "control readiness deadline expired: probe execution failed "
        f"(exit {result.returncode}) for {expected_node}: {output}"
    )


def wait_for_celery_worker(
    *,
    expected_node: str,
    worker_pid: int,
    timeout_seconds: float,
    interval_seconds: float,
    probe: Callable[[float], ProbeResult],
    process_alive: Callable[[int], bool],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Wait only until the expected alive worker answers Celery control ping."""
    deadline = monotonic() + timeout_seconds
    attempt = 0
    last_result: ProbeResult | None = None

    while True:
        if not process_alive(worker_pid):
            return (
                False,
                f"worker process exited during control readiness (pid {worker_pid})",
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            break

        attempt += 1
        # A control request published before the worker's pidbox consumer is
        # ready can receive no reply. Keep each request short so one lost
        # startup probe cannot consume the complete readiness deadline. Half
        # of the remaining deadline is reserved for later polling/failure
        # accounting even in the final portion of the window.
        result = probe(min(MAX_PROBE_SECONDS, remaining / 2))
        last_result = result
        nodes = _responding_nodes(result.output)
        if result.returncode == 0 and expected_node in nodes:
            unexpected = sorted(nodes - {expected_node})
            suffix = (
                f"; additional responding nodes: {', '.join(unexpected)}"
                if unexpected
                else ""
            )
            return (
                True,
                f"expected worker {expected_node} responded on attempt {attempt}{suffix}",
            )

        if not process_alive(worker_pid):
            return (
                False,
                f"worker process exited during control readiness (pid {worker_pid})",
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(interval_seconds, remaining))

    if last_result is None:
        return (
            False,
            f"control readiness deadline expired before a probe for {expected_node}",
        )

    nodes = _responding_nodes(last_result.output)
    if nodes and expected_node not in nodes:
        return (
            False,
            "control readiness deadline expired: unexpected responding node(s) "
            f"{', '.join(sorted(nodes))}; expected {expected_node}; "
            f"last probe exit {last_result.returncode}: {_summarize_output(last_result.output)}",
        )
    if last_result.returncode != 0:
        return False, _timeout_diagnostic(expected_node, last_result)
    return (
        False,
        f"control readiness deadline expired: no response from expected worker {expected_node}; "
        f"last probe: {_summarize_output(last_result.output)}",
    )


def _worker_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _celery_probe(celery: str, expected_node: str, timeout: float) -> ProbeResult:
    control_timeout = max(timeout - PROBE_TEARDOWN_MARGIN_SECONDS, 0.05)
    command = [
        celery,
        "-A",
        "app.celery_app",
        "inspect",
        "ping",
        "--destination",
        expected_node,
        "--timeout",
        str(control_timeout),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            # The CLI teardown margin is contained inside the caller's
            # per-probe budget, never added beyond the overall deadline.
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return ProbeResult(returncode=124, output=output or "probe command timed out")
    except OSError as exc:
        return ProbeResult(
            returncode=127, output=f"could not execute Celery probe: {exc}"
        )
    return ProbeResult(completed.returncode, completed.stdout + completed.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--celery", required=True, help="path to the Celery executable")
    parser.add_argument(
        "--pid", required=True, type=int, help="PID of the worker started by start.sh"
    )
    parser.add_argument(
        "--expected-node", required=True, help="canonical Celery worker node name"
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("timeout and interval must be positive")

    succeeded, diagnostic = wait_for_celery_worker(
        expected_node=args.expected_node,
        worker_pid=args.pid,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
        probe=lambda remaining: _celery_probe(
            args.celery, args.expected_node, remaining
        ),
        process_alive=_worker_alive,
    )
    stream = sys.stdout if succeeded else sys.stderr
    print(f"Celery control readiness: {diagnostic}", file=stream)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
