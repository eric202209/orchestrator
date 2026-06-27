"""Small cross-platform advisory file-lock compatibility layer."""

from __future__ import annotations

import errno
import os

try:  # pragma: no cover - exercised on POSIX hosts
    import fcntl as fcntl  # type: ignore[no-redef]
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows hosts
    import msvcrt

    class _WindowsFcntlCompat:
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(fd: int, flags: int) -> None:
            mode = msvcrt.LK_UNLCK
            if flags & _WindowsFcntlCompat.LOCK_UN:
                mode = msvcrt.LK_UNLCK
            elif flags & _WindowsFcntlCompat.LOCK_NB:
                mode = msvcrt.LK_NBLCK
            else:
                mode = msvcrt.LK_LOCK

            position = os.lseek(fd, 0, os.SEEK_CUR)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, mode, 1)
            except OSError as exc:
                if flags & _WindowsFcntlCompat.LOCK_NB:
                    exc.errno = exc.errno or errno.EACCES
                raise
            finally:
                os.lseek(fd, position, os.SEEK_SET)

    fcntl = _WindowsFcntlCompat()
