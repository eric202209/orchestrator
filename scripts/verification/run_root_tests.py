"""Run only the standalone root-verifier suite without app conftest or plugins."""

from __future__ import annotations

import importlib.abc
import os
from pathlib import Path
import sys
import tempfile


class NoAppImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app" or fullname.startswith("app."):
            raise RuntimeError("ISOLATION_GUARD: application import forbidden")
        return None


def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        name = Path(os.fsdecode(args[0])).name
        if name in (".env", "openclaw.json", "auth-profiles.json", "orchestrator.db"):
            raise RuntimeError(
                "ISOLATION_GUARD: production configuration/state read forbidden"
            )
    if event in (
        "socket.connect",
        "socket.bind",
        "os.kill",
        "os.killpg",
        "subprocess.Popen",
        "os.system",
    ):
        raise RuntimeError("ISOLATION_GUARD: external effect forbidden")


def main():
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, NoAppImports())
    sys.addaudithook(audit)
    test_path = (
        Path(__file__).resolve().parents[2] / "app/tests/test_phase37_dp3_m1_roots.py"
    )
    with tempfile.TemporaryDirectory(prefix="dp3-root-harness-") as scratch:
        os.environ["HOME"] = scratch
        os.environ["TMPDIR"] = scratch
        tempfile.tempdir = scratch
        import pytest

        result = pytest.main(
            [
                "--noconftest",
                "-c",
                "/dev/null",
                "-p",
                "no:cacheprovider",
                "--import-mode=importlib",
                str(test_path),
                *sys.argv[1:],
            ]
        )
        if any(name == "app" or name.startswith("app.") for name in sys.modules):
            raise RuntimeError("ISOLATION_GUARD: application module was loaded")
        print("ISOLATION_GUARD=PASS; no app imports or forbidden effects")
        return result


if __name__ == "__main__":
    sys.exit(main())
