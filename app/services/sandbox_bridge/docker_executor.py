"""Fixed-profile Docker execution. Requests cannot influence Docker argv."""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import subprocess
import time
import uuid

from .contract import ExecutionRequest, ExecutionResult, MAX_OUTPUT_BYTES
from .staging import StagedWorkspace

IMAGE = "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"


class DockerExecutor:
    def __init__(self, staging_root: str | Path, *, docker: str = "docker"):
        self.staging_root, self.docker = Path(staging_root), docker

    def _argv(self, name: str, workspace: Path) -> list[str]:
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
        name = "p43-sbx-" + uuid.uuid4().hex
        staged = None
        proc = None
        out, err = bytearray(), bytearray()
        truncated = False
        status, code, error = "EXECUTION_FAILED", None, None
        manifest, artifacts = [], []
        cleanup_ok = True
        try:
            staged = StagedWorkspace(self.staging_root, request.code)
            if "," in str(staged.path):
                raise RuntimeError("unsafe_docker_mount_path")
            proc = subprocess.Popen(
                self._argv(name, staged.path),
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
            if status == "SUCCESS":
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
