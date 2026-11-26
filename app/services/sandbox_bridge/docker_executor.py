"""Fixed-profile Docker execution. Requests cannot influence Docker argv."""

from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import uuid

from .contract import (
    PYTEST_PROFILE,
    ExecutionRequest,
    ExecutionResult,
    MAX_OUTPUT_BYTES,
    decode_workspace_snapshot,
)
from .staging import StagedWorkspace

IMAGE = "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
# P43-PRODUCT-23A: local image ID (``sha256:<64 hex>``) of the image built from
# scripts/sandbox/pytest-image/ on the Host. PRODUCT-23B Stage Y pins the GX10
# build of Stage X (0d2d297); a missing or different local image fails closed.
PYTEST_IMAGE: str | None = (
    "sha256:4fe5ac69330c35161326f6737210c24163ed525a230a24d894b1cb3b1b2fd42b"
)
_PINNED_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Fixed in-container bootstrap, run with cwd /ws/src. The request supplies only
# /ws/args.json, read before any workspace code runs. ``compileall`` mirrors
# ``python -m compileall -q`` (exit 1 on any syntax error) without writing
# bytecode, since the snapshot is read-only.
PYTEST_BOOTSTRAP = (
    "import json,sys\n"
    "spec=json.load(open('../args.json',encoding='utf-8'))\n"
    "if spec['tool']=='compileall':\n"
    "    import traceback\n"
    "    failed=0\n"
    "    for path in spec['args']:\n"
    "        try:\n"
    "            compile(open(path,'rb').read(),path,'exec',dont_inherit=True)\n"
    "        except (SyntaxError,ValueError,OSError):\n"
    "            failed=1\n"
    "            traceback.print_exc(limit=0)\n"
    "    sys.exit(failed)\n"
    "import pytest\n"
    "sys.exit(int(pytest.main(['-p','no:cacheprovider',*spec['args']])))\n"
)


def pinned_pytest_image() -> str | None:
    image = PYTEST_IMAGE
    return image if isinstance(image, str) and _PINNED_IMAGE.fullmatch(image) else None


class DockerExecutor:
    def __init__(
        self,
        staging_root: str | Path,
        *,
        docker: str = "docker",
        name_prefix: str = "p43-sbx-",
    ):
        self.staging_root, self.docker = Path(staging_root), docker
        self.name_prefix = name_prefix

    def _argv(
        self, name: str, workspace: Path, profile: str | None = None
    ) -> list[str]:
        if profile == PYTEST_PROFILE:
            return self._pytest_argv(name, workspace)
        return [
            self.docker,
            "run",
            "--pull=never",
            "--platform",
            "linux/arm64",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--ipc=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--cpus=1",
            "--memory=512m",
            "--memory-swap=512m",
            "--pids-limit=64",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            "fsize=16777216:16777216",
            "--mount",
            f"type=bind,src={workspace},dst=/ws",
            "--workdir=/ws/output",
            "--entrypoint",
            "/usr/bin/env",
            IMAGE,
            "-i",
            "HOME=/ws",
            "TMPDIR=/ws/output",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "LC_CTYPE=C.UTF-8",
            "python3",
            "-I",
            "/ws/input.py",
        ]

    def _pytest_argv(self, name: str, workspace: Path) -> list[str]:
        # Same fixed isolation as the script profile. The bind is read-only
        # and the only writable path is a size-capped noexec tmpfs, so nothing
        # the sandbox writes survives it or reaches the Worker.
        image = pinned_pytest_image()
        if image is None:
            raise RuntimeError("pytest_image_unpinned")
        return [
            self.docker,
            "run",
            "--pull=never",
            "--platform",
            "linux/arm64",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--ipc=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--cpus=1",
            "--memory=512m",
            "--memory-swap=512m",
            "--pids-limit=64",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            "fsize=16777216:16777216",
            "--mount",
            f"type=bind,src={workspace},dst=/ws,readonly",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--workdir=/ws/src",
            "--entrypoint",
            "/usr/bin/env",
            image,
            "-i",
            "HOME=/tmp",
            "TMPDIR=/tmp",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "LC_CTYPE=C.UTF-8",
            "python3",
            "-B",
            "-E",
            "-s",
            "-c",
            PYTEST_BOOTSTRAP,
        ]

    def _docker_control(self, *args: str) -> bool:
        try:
            return (
                subprocess.run(
                    [self.docker, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

    def execute(self, request: ExecutionRequest, cancel) -> ExecutionResult:
        started = time.monotonic()
        name = self.name_prefix + uuid.uuid4().hex
        staged = None
        proc = None
        out, err = bytearray(), bytearray()
        truncated = False
        status, code, error = "EXECUTION_FAILED", None, None
        manifest, artifacts = [], []
        cleanup_ok = True
        pytest_profile = request.execution_profile == PYTEST_PROFILE
        if pytest_profile and pinned_pytest_image() is None:
            return ExecutionResult(
                request.request_id,
                "REJECTED",
                None,
                "",
                "",
                error_code="pytest_image_unpinned",
            )
        try:
            snapshot = (
                decode_workspace_snapshot(request.workspace_snapshot)[0]
                if pytest_profile
                else None
            )
            staged = StagedWorkspace(self.staging_root, request.code, snapshot)
            if "," in str(staged.path):
                raise RuntimeError("unsafe_docker_mount_path")
            proc = subprocess.Popen(
                self._argv(name, staged.path, request.execution_profile),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
                env={"PATH": "/usr/bin:/bin"},
            )
            selector = selectors.DefaultSelector()
            for stream, target in ((proc.stdout, out), (proc.stderr, err)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, target)
            deadline = started + request.timeout_seconds
            timed_out = cancelled = False
            while proc.poll() is None or selector.get_map():
                if cancel.is_set():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = key.data
                    if len(target) < MAX_OUTPUT_BYTES:
                        target.extend(chunk[: MAX_OUTPUT_BYTES - len(target)])
                    if len(target) + len(chunk) > MAX_OUTPUT_BYTES:
                        truncated = True
            if timed_out or cancelled:
                self._docker_control("stop", "--time", "2", name)
                try:
                    proc.wait(timeout=2.5)
                except subprocess.TimeoutExpired:
                    self._docker_control("kill", name)
                    proc.kill()
                    proc.wait(timeout=2)
                status = "TIMEOUT" if timed_out else "CANCELLED"
            else:
                code = proc.wait()
                status = (
                    "SUCCESS"
                    if code == 0
                    else ("UNAVAILABLE" if code == 125 else "EXECUTION_FAILED")
                )
                if code == 125:
                    error = "docker_runtime_unavailable"
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            if status == "SUCCESS" and not pytest_profile:
                # v3 returns no artifacts: sandbox files never propagate.
                manifest, artifacts = staged.collect()
        except FileNotFoundError:
            status, error = "UNAVAILABLE", "docker_unavailable"
        except Exception as exc:
            status, error = "INTERNAL_ERROR", type(exc).__name__
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    self._docker_control("stop", "--time", "1", name)
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    cleanup_ok = False
            if proc is not None and not self._docker_control("rm", "-f", name):
                # A failed rm is safe only when inspect explicitly confirms absence.
                try:
                    inspected = subprocess.run(
                        [self.docker, "inspect", name],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=3,
                        text=True,
                        check=False,
                    )
                    inspect_error = (inspected.stderr + inspected.stdout).lower()
                    absent = inspected.returncode != 0 and (
                        "no such object" in inspect_error
                        or "no such container" in inspect_error
                    )
                    cleanup_ok = absent
                except Exception:
                    cleanup_ok = False
            if staged is not None:
                try:
                    staged.cleanup()
                except OSError:
                    cleanup_ok = False
        if not cleanup_ok:
            status, error, manifest, artifacts = (
                "INTERNAL_ERROR",
                "cleanup_failed",
                [],
                [],
            )
        return ExecutionResult(
            request.request_id,
            status,
            code,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
            truncated,
            error,
            time.monotonic() - started,
            manifest,
            artifacts,
        )
