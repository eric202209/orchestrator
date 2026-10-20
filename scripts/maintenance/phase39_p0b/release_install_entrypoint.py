"""Root entry point for installing an inert release from an existing release."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


TRUSTED_BASE = Path("/usr/local/lib/orchestrator-maintenance/phase39-p0b")


def _require_trusted_installer() -> Path:
    if sys.flags.isolated != 1 or os.geteuid() != 0:
        raise RuntimeError("protected_installer_requires_root_isolated_python")
    script = Path(__file__).absolute()
    release_root = script.parents[3]
    if release_root.parent != TRUSTED_BASE:
        raise RuntimeError("installer_not_under_protected_release_root")
    current = Path("/")
    for part in script.parts[1:-1]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("installer_parent_directory_unprotected")
    script_info = script.lstat()
    if not stat.S_ISREG(script_info.st_mode) or script_info.st_uid != 0 or script_info.st_nlink != 1 or script_info.st_mode & 0o222:
        raise RuntimeError("installer_script_unprotected")
    for current_dir, dirs, files in os.walk(release_root, followlinks=False):
        directory = Path(current_dir)
        dir_info = directory.lstat()
        if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != 0 or dir_info.st_mode & 0o022:
            raise RuntimeError("installer_release_directory_unprotected")
        for name in dirs:
            child = directory / name
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError("installer_release_subdirectory_unprotected")
        for name in files:
            child = directory / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
                raise RuntimeError("installer_release_dependency_unprotected")
    return release_root


def main() -> int:
    release_root = _require_trusted_installer()
    sys.path.insert(0, str(release_root))
    from scripts.maintenance.phase39_p0b import release_manager

    return release_manager._cli()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
