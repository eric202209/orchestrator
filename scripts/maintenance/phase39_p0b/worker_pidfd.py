"""Fail-closed Linux pidfd operations for the Phase 39 offline tooling."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import select
import stat
import time
from typing import Callable


class PidfdError(RuntimeError):
    """A pidfd operation failed without a numeric-PID fallback."""


class UnsupportedPidfdError(PidfdError):
    """The runtime does not expose the required Linux pidfd operation."""


_PIDFD_SEND_SIGNAL_SYSCALLS = {
    # Linux's generic syscall table and x86-64 table both assign 424.
    "aarch64": 424,
    "x86_64": 424,
}


class PidfdOperations:
    """Open, validate, signal, and wait through Linux process descriptors.

    `pidfd_open` comes from Python's os module. Python versions without a
    `pidfd_send_signal` wrapper use libc.syscall only on the explicitly
    supported Linux architecture table above. Other systems fail closed.
    """

    def __init__(
        self,
        *,
        system: str | None = None,
        machine: str | None = None,
        proc_root: str = "/proc",
        pidfd_open_fn: Callable[..., int] | None = None,
        syscall_fn: Callable[[int, int, int, object | None, int], int] | None = None,
        poll_factory: Callable[[], select.poll] = select.poll,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.system = system or platform.system()
        self.machine = machine or platform.machine()
        self.proc_root = proc_root
        self.pidfd_open_fn = (
            getattr(os, "pidfd_open", None) if pidfd_open_fn is None else pidfd_open_fn
        )
        self.syscall_fn = syscall_fn
        self.poll_factory = poll_factory
        self.monotonic = monotonic

    def _require_linux(self) -> None:
        if self.system != "Linux":
            raise UnsupportedPidfdError("pidfd operations require Linux")

    def open(self, pid: int) -> int:
        self._require_linux()
        if self.pidfd_open_fn is None:
            raise UnsupportedPidfdError("Python os.pidfd_open is unavailable")
        try:
            fd = self.pidfd_open_fn(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                raise UnsupportedPidfdError("kernel pidfd_open syscall is unsupported") from exc
            raise
        if type(fd) is not int or fd < 0:
            raise PidfdError("pidfd_open returned an invalid descriptor")
        return fd

    def target_pid(self, pidfd: int) -> int:
        self._require_linux()
        path = os.path.join(self.proc_root, "self", "fdinfo", str(pidfd))
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PidfdError("pidfd fdinfo is not a regular procfs record")
            raw = os.read(fd, 8192)
        finally:
            os.close(fd)
        matches = [line for line in raw.decode("ascii", "strict").splitlines() if line.startswith("Pid:")]
        if len(matches) != 1:
            raise PidfdError("pidfd target PID is unavailable or ambiguous")
        try:
            target = int(matches[0].split(":", 1)[1].strip())
        except ValueError as exc:
            raise PidfdError("pidfd target PID is malformed") from exc
        if target < -1:
            raise PidfdError("pidfd target PID is invalid")
        return target

    def is_exited(self, pidfd: int) -> bool:
        target = self.target_pid(pidfd)
        if target == -1:
            return True
        poller = self.poll_factory()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        events = poller.poll(0)
        for descriptor, mask in events:
            if descriptor != pidfd:
                continue
            if mask & select.POLLNVAL:
                raise PidfdError("pidfd is invalid")
            if mask & (select.POLLIN | select.POLLHUP | select.POLLERR):
                return True
        return False

    def _send_signal_syscall(self, number: int, pidfd: int, signum: int) -> int:
        if self.syscall_fn is not None:
            try:
                return self.syscall_fn(number, pidfd, signum, None, 0)
            except OSError as exc:
                if exc.errno == errno.ENOSYS:
                    raise UnsupportedPidfdError(
                        "kernel pidfd_send_signal syscall is unsupported"
                    ) from exc
                raise
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        result = syscall(
            ctypes.c_long(number),
            ctypes.c_int(pidfd),
            ctypes.c_int(signum),
            ctypes.c_void_p(),
            ctypes.c_uint(0),
        )
        if result < 0:
            error_number = ctypes.get_errno()
            if error_number == errno.ENOSYS:
                raise UnsupportedPidfdError("kernel pidfd_send_signal syscall is unsupported")
            raise OSError(error_number, os.strerror(error_number))
        return int(result)

    def send_signal(self, pidfd: int, signum: int) -> None:
        self._require_linux()
        try:
            number = _PIDFD_SEND_SIGNAL_SYSCALLS[self.machine]
        except KeyError as exc:
            raise UnsupportedPidfdError(
                f"pidfd_send_signal syscall number is unverified for {self.machine}"
            ) from exc
        result = self._send_signal_syscall(number, pidfd, signum)
        if result != 0:
            raise PidfdError("pidfd_send_signal returned an unexpected result")

    def wait_for_exit(self, pidfd: int, timeout_seconds: float) -> bool:
        self._require_linux()
        if timeout_seconds < 0:
            raise ValueError("pidfd wait timeout must not be negative")
        poller = self.poll_factory()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        deadline = self.monotonic() + timeout_seconds
        while True:
            remaining = max(0.0, deadline - self.monotonic())
            timeout_ms = int(remaining * 1000 + 0.999)
            events = poller.poll(timeout_ms)
            for descriptor, mask in events:
                if descriptor != pidfd:
                    continue
                if mask & select.POLLNVAL:
                    raise PidfdError("pidfd became invalid while waiting")
                if mask & (select.POLLIN | select.POLLHUP | select.POLLERR):
                    return True
            if self.monotonic() >= deadline:
                return False

    @staticmethod
    def close(pidfd: int) -> None:
        os.close(pidfd)
