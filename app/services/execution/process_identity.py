"""Process-start identity helpers shared by runtime and recovery paths."""

from __future__ import annotations

import os
import socket
import time
import uuid
from pathlib import Path


_WORKER_IDENTITIES: dict[int, tuple[str, str, str, int]] = {}


def current_process_start_identity(pid: int | None = None) -> str | None:
    """Return a PID-reuse-safe identity for a Linux process, when observable.

    Linux ``/proc/<pid>/stat`` field 22 is the process start time in clock
    ticks.  Pairing it with the kernel boot id prevents a PID reused after a
    reboot from being mistaken for the recorded owner.  Recovery callers treat
    an unreadable proc record as unknown rather than guessing.
    """

    observed_pid = os.getpid() if pid is None else int(pid)
    if observed_pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{observed_pid}/stat").read_text(encoding="utf-8")
        closing_paren = stat.rfind(")")
        if closing_paren < 0:
            return None
        fields_after_comm = stat[closing_paren + 2 :].split()
        # The slice starts at field 3 (state); field 22 is offset 19.
        start_ticks = fields_after_comm[19]
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
        if not boot_id or not start_ticks:
            return None
        return f"{boot_id}:{start_ticks}"
    except (OSError, IndexError, ValueError):
        return None


def current_worker_identity() -> tuple[str, str, str, int]:
    """Return a stable worker identity, including after Celery forks."""

    pid = os.getpid()
    cached = _WORKER_IDENTITIES.get(pid)
    if cached is not None:
        return cached
    process_start_identity = current_process_start_identity(pid)
    if process_start_identity is None:
        # The canonical lease schema requires a non-empty value.  The explicit
        # marker remains distinguishable from an observed identity, so recovery
        # will fail safe if /proc cannot later be inspected.
        process_start_identity = (
            f"unavailable:{pid}:{time.time_ns()}:{uuid.uuid4().hex}"
        )
    hostname = socket.gethostname()
    identity = (
        f"{hostname}-{pid}-{process_start_identity}",
        hostname,
        process_start_identity,
        pid,
    )
    _WORKER_IDENTITIES[pid] = identity
    return identity
