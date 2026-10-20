from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from scripts.maintenance.phase39_p0b import operator_tooling as tooling
from scripts.maintenance.phase39_p0b import (
    protected_refresh_authority as authority_module,
)
from scripts.maintenance.phase39_p0b import worker_pidfd

TEST_SOURCE_ROOT = Path(__file__).resolve().parents[2]
tooling.APPROVED_SOURCE_ROOT = str(TEST_SOURCE_ROOT)
tooling.APPROVED = replace(tooling.APPROVED, source_root=str(TEST_SOURCE_ROOT))


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
BOOT = "11111111-1111-4111-8111-111111111111"


def base_receipt(root: Path, refresh_id: str) -> dict:
    source_root = root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    worker = {
        "pid": 311,
        "start_ticks": "4100",
        "linux_boot_id": BOOT,
        "executable": "/usr/bin/python3.12",
        "cwd": str(source_root),
        "argv": [
            str(source_root / "venv/bin/python3"),
            "-A",
            "app.celery_app",
            "worker",
        ],
    }
    beat = {
        "role": "celery-beat",
        "pid": 312,
        "start_ticks": "4200",
        "linux_boot_id": BOOT,
        "executable": "/usr/bin/python3.12",
        "cwd": str(source_root),
        "argv": [str(source_root / "venv/bin/python3"), "-A", "app.celery_app", "beat"],
    }
    return {
        "receipt_schema_version": authority_module.REFRESH_RECEIPT_SCHEMA_VERSION,
        "refresh_id": refresh_id,
        "source_identity": {
            "source_root": str(source_root),
            "head": "a" * 40,
            "tree": "b" * 40,
            "source_manifest_digest": "c" * 64,
            "configuration_fingerprint": "d" * 64,
            "configuration_schema_version": 2,
            "configuration_policy_id": "phase37-test-policy-v2",
        },
        "launcher_identity": {
            "launcher": "start.sh:start_worker_only",
            "executable": "/usr/bin/python3.12",
            "cwd": str(source_root),
            "argv": [
                str(source_root / "venv/bin/python3"),
                "-A",
                "app.celery_app",
                "worker",
            ],
        },
        "installation": "INSTALLED",
        "approval_id": "P39-P0B-TEST-001",
        "pin_sha256": "e" * 64,
        "pin_path": str(root / "pin.json"),
        "installation_receipt_sha256": "f" * 64,
        "worker_node_identity": "celery@test-node",
        "pre_signal_worker_identity": worker,
        "beat_process_identity": beat,
        "pre_signal_observed_at": "2026-10-20T03:59:00.000000Z",
    }


def append_completed(
    authority: authority_module.RefreshAuthority,
    refresh_id: str,
    receipt: dict,
    root: Path,
):
    ready_at = "2026-10-20T03:59:30.000000Z"
    launch_at = "2026-10-20T03:59:10.000000Z"
    new_worker = {
        "pid": 411,
        "start_ticks": "5100",
        "linux_boot_id": BOOT,
        "executable": "/usr/bin/python3.12",
        "cwd": receipt["source_identity"]["source_root"],
        "argv": receipt["launcher_identity"]["argv"],
    }
    with authority.locked(exclusive=True, create=True) as directory_fd:
        authority.begin(directory_fd, refresh_id, receipt)
        authority.append_stage(
            directory_fd,
            refresh_id,
            "SIGTERM_SENT",
            {"signum": 15, "pidfd_bound": True},
        )
        authority.append_stage(
            directory_fd,
            refresh_id,
            "WORKER_EXITED",
            {
                "pid": receipt["pre_signal_worker_identity"]["pid"],
                "start_ticks": receipt["pre_signal_worker_identity"]["start_ticks"],
            },
        )
        authority.append_stage(
            directory_fd, refresh_id, "LAUNCH_STARTED", {"launch_started_at": launch_at}
        )
        authority.append_stage(
            directory_fd,
            refresh_id,
            "LAUNCH_ATTEMPTED",
            {"worker_pid": new_worker["pid"], "returncode": 0, "launch_attempts": 1},
        )
        authority.append_stage(
            directory_fd,
            refresh_id,
            "PROCESS_OBSERVED",
            {"worker_process_identity": new_worker},
        )
        authority.append_stage(
            directory_fd,
            refresh_id,
            "IDENTITY_VERIFIED",
            {"worker_process_identity": new_worker},
        )
        return authority.append_stage(
            directory_fd,
            refresh_id,
            "READY",
            {
                "worker_process_identity": new_worker,
                "master_pid": new_worker["pid"],
                "worker_start_ticks": new_worker["start_ticks"],
                "linux_boot_id": new_worker["linux_boot_id"],
                "manifest_path": str(root / "manifest.json"),
                "manifest_record_id": f"{BOOT}-{new_worker['pid']}-{new_worker['start_ticks']}",
                "manifest_sha256": "1" * 64,
                "manifest_mtime_ns": 1_900_000_000_000_000_000,
                "launch_started_at": launch_at,
                "ready_at": ready_at,
                "ping": "OK",
                "launcher_returncode": 0,
            },
        )


class ProtectedRefreshAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner_uid = os.geteuid()
        self.temp = tempfile.TemporaryDirectory(prefix="p39-authority-test-")
        self.root = Path(self.temp.name)
        self.authority_root = self.root / "authority"
        self.authority = authority_module.RefreshAuthority(
            self.authority_root,
            required_uid=self.owner_uid,
            clock=lambda: NOW,
        )
        self.refresh_id = str(uuid.uuid4())
        self.receipt = base_receipt(self.root, self.refresh_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_canonical_receipt_hash_unique_id_and_append_only_transitions(self) -> None:
        with self.authority.locked(exclusive=True, create=True) as directory_fd:
            record = self.authority.begin(directory_fd, self.refresh_id, self.receipt)
            self.assertEqual(record.last_stage, "PREPARED")
            self.assertEqual(
                record.receipt_sha256,
                hashlib.sha256(
                    authority_module.canonical_json_bytes(self.receipt)
                ).hexdigest(),
            )
            before = (self.authority_root / f"{self.refresh_id}.jsonl").read_bytes()
            with self.assertRaisesRegex(
                authority_module.RefreshAuthorityError, "immutable history"
            ):
                self.authority.begin(directory_fd, self.refresh_id, self.receipt)
            with self.assertRaisesRegex(
                authority_module.RefreshAuthorityError, "out of order"
            ):
                self.authority.append_stage(directory_fd, self.refresh_id, "READY", {})
            self.assertEqual(
                (self.authority_root / f"{self.refresh_id}.jsonl").read_bytes(), before
            )

    def test_recovery_or_incomplete_history_never_produces_ready_receipt(self) -> None:
        with self.authority.locked(exclusive=True, create=True) as directory_fd:
            self.authority.begin(directory_fd, self.refresh_id, self.receipt)
            record = self.authority.append_stage(
                directory_fd,
                self.refresh_id,
                "RECOVERY_REQUIRED",
                {"reason": "injected post-signal failure"},
            )
            self.assertEqual(record.last_stage, "RECOVERY_REQUIRED")
            with self.assertRaisesRegex(
                authority_module.RefreshAuthorityError, "complete READY"
            ):
                self.authority.final_receipt(record)
            with self.assertRaisesRegex(
                authority_module.RefreshAuthorityError, "terminal"
            ):
                self.authority.append_stage(
                    directory_fd,
                    self.refresh_id,
                    "READY",
                    {},
                )
        with self.authority.locked(exclusive=False, create=False) as directory_fd:
            with self.assertRaisesRegex(
                authority_module.RefreshAuthorityError, "manual reconciliation"
            ):
                self.authority.assert_no_unresolved_history(directory_fd)

    def test_truncated_and_hash_altered_records_fail_closed(self) -> None:
        with self.authority.locked(exclusive=True, create=True) as directory_fd:
            self.authority.begin(directory_fd, self.refresh_id, self.receipt)
        record_path = self.authority_root / f"{self.refresh_id}.jsonl"
        raw = record_path.read_bytes()
        record_path.write_bytes(raw[:-3])
        with self.assertRaisesRegex(
            authority_module.RefreshAuthorityError, "truncated"
        ):
            self.authority.load(self.refresh_id)
        record_path.write_bytes(raw)
        event = json.loads(raw)
        event["payload"]["receipt"]["beat_process_identity"]["pid"] = 999
        record_path.write_bytes(authority_module.canonical_json_bytes(event) + b"\n")
        with self.assertRaisesRegex(
            authority_module.RefreshAuthorityError, "integrity check"
        ):
            self.authority.load(self.refresh_id)

    def test_wrong_refresh_id_and_conflicting_history_name_fail_closed(self) -> None:
        with self.authority.locked(exclusive=True, create=True) as directory_fd:
            self.authority.begin(directory_fd, self.refresh_id, self.receipt)
        with self.assertRaisesRegex(authority_module.RefreshAuthorityError, "missing"):
            self.authority.load(str(uuid.uuid4()))
        duplicate_name = f"{self.refresh_id.upper()}.jsonl"
        fd = os.open(
            self.authority_root / duplicate_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(fd)
        with self.assertRaisesRegex(
            authority_module.RefreshAuthorityError, "conflicting"
        ):
            self.authority.load(self.refresh_id)

    def test_symlink_directory_and_journal_are_rejected(self) -> None:
        actual = self.root / "actual-authority"
        actual_authority = authority_module.RefreshAuthority(
            actual, required_uid=self.owner_uid, clock=lambda: NOW
        )
        with actual_authority.locked(exclusive=True, create=True) as directory_fd:
            actual_authority.begin(directory_fd, self.refresh_id, self.receipt)
        linked = self.root / "linked-authority"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(authority_module.RefreshAuthorityError):
            authority_module.RefreshAuthority(linked, required_uid=self.owner_uid).load(
                self.refresh_id
            )

        record_path = actual / f"{self.refresh_id}.jsonl"
        target = self.root / "target.jsonl"
        target.write_bytes(record_path.read_bytes())
        os.chmod(target, 0o600)
        record_path.unlink()
        record_path.symlink_to(target)
        with self.assertRaisesRegex(
            authority_module.RefreshAuthorityError, "unreadable"
        ):
            actual_authority.load(self.refresh_id)

    def test_owner_and_mode_checks_and_nonblocking_concurrency_lock(self) -> None:
        with self.authority.locked(exclusive=True, create=True) as directory_fd:
            self.authority.begin(directory_fd, self.refresh_id, self.receipt)
            with self.assertRaisesRegex(authority_module.RefreshAuthorityError, "lock"):
                with self.authority.locked(exclusive=True, create=False):
                    pass
        journal = self.authority_root / f"{self.refresh_id}.jsonl"
        os.chmod(journal, 0o640)
        with self.assertRaisesRegex(authority_module.RefreshAuthorityError, "mode"):
            self.authority.load(self.refresh_id)
        os.chmod(journal, 0o600)
        os.chmod(self.authority_root, 0o750)
        with self.assertRaisesRegex(
            authority_module.RefreshAuthorityError, "directory mode"
        ):
            self.authority.load(self.refresh_id)

    def test_pre_signal_create_and_each_transition_fsync(self) -> None:
        # The injected callback verifies that the intended descriptors are
        # passed to fsync; it does not simulate power loss or prove storage
        # hardware has made those writes durable.
        fsync_fds: list[int] = []
        authority = authority_module.RefreshAuthority(
            self.authority_root,
            required_uid=self.owner_uid,
            clock=lambda: NOW,
            fsync=lambda fd: fsync_fds.append(fd),
        )
        with authority.locked(exclusive=True, create=True) as directory_fd:
            fsync_fds.clear()
            authority.begin(directory_fd, self.refresh_id, self.receipt)
            self.assertEqual(
                len(fsync_fds), 2, "PREPARED record and parent directory must be synced"
            )
            authority.append_stage(
                directory_fd,
                self.refresh_id,
                "SIGTERM_SENT",
                {"signum": 15, "pidfd_bound": True},
            )
            self.assertEqual(len(fsync_fds), 3, "stage append must be synced")


class PidfdOperationsTests(unittest.TestCase):
    @staticmethod
    def _fixture_code() -> str:
        return (
            "import signal, sys, time\n"
            "from pathlib import Path\n"
            "marker = Path(sys.argv[1])\n"
            "def on_term(_sig, _frame):\n"
            "    with marker.open('a', encoding='utf-8') as out: out.write('SIGTERM\\n')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('READY', flush=True)\n"
            "while True: time.sleep(0.05)\n"
        )

    def _start_fixture(self, marker: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", self._fixture_code(), str(marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = process.stdout.readline().strip() if process.stdout else ""
        if ready != "READY":
            raise AssertionError(
                f"isolated process fixture did not become ready: {ready!r}"
            )
        return process

    @unittest.skipUnless(sys.platform.startswith("linux"), "pidfd requires Linux")
    def test_real_pidfd_sigterm_reaches_only_the_bound_subprocess_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p39-pidfd-test-") as temp:
            root = Path(temp)
            target_marker = root / "target.signal"
            unrelated_marker = root / "unrelated.signal"
            target = self._start_fixture(target_marker)
            unrelated = self._start_fixture(unrelated_marker)
            ops = worker_pidfd.PidfdOperations()
            observer = tooling.ProcfsObserver()
            target_fd = unrelated_fd = None
            try:
                expected = observer.identity(target.pid)
                self.assertIsNotNone(expected)
                target_fd = ops.open(target.pid)
                unrelated_fd = ops.open(unrelated.pid)
                tooling._validate_pidfd_target(
                    ops, target_fd, expected, observer, "isolated target"
                )
                self.assertEqual(ops.target_pid(target_fd), target.pid)
                ops.send_signal(target_fd, signal.SIGTERM)
                self.assertTrue(ops.wait_for_exit(target_fd, 5.0))
                self.assertEqual(target.wait(timeout=5.0), 0)
                self.assertEqual(
                    target_marker.read_text(encoding="utf-8").splitlines(), ["SIGTERM"]
                )
                self.assertIsNone(unrelated.poll())
                self.assertFalse(unrelated_marker.exists())
                self.assertEqual(ops.target_pid(target_fd), -1)
                with self.assertRaises(ProcessLookupError):
                    ops.send_signal(target_fd, signal.SIGTERM)

                # Cleanup is also bound to the isolated fixture's own pidfd.
                ops.send_signal(unrelated_fd, signal.SIGTERM)
                self.assertTrue(ops.wait_for_exit(unrelated_fd, 5.0))
                self.assertEqual(unrelated.wait(timeout=5.0), 0)
                self.assertEqual(
                    unrelated_marker.read_text(encoding="utf-8").splitlines(),
                    ["SIGTERM"],
                )
            finally:
                if target_fd is not None:
                    ops.close(target_fd)
                if unrelated_fd is not None:
                    ops.close(unrelated_fd)
                if target.poll() is None:
                    target.kill()
                    target.wait(timeout=5.0)
                if unrelated.poll() is None:
                    unrelated.kill()
                    unrelated.wait(timeout=5.0)
                for process in (target, unrelated):
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()

    def test_invalid_descriptor_unsupported_arch_enosys_and_permission_fail_closed(
        self,
    ) -> None:
        ops = worker_pidfd.PidfdOperations()
        with self.assertRaises(OSError):
            ops.target_pid(-1)
        with self.assertRaises(OSError):
            ops.send_signal(-1, signal.SIGTERM)

        # Injected syscall functions exercise dispatch and errno mapping only;
        # they do not prove the kernel ABI. The isolated real-process test
        # above verifies the configured syscall on this Linux runtime.
        calls: list[tuple[int, int, int]] = []
        unsupported_arch = worker_pidfd.PidfdOperations(
            system="Linux",
            machine="unverified-arch",
            syscall_fn=lambda *args: calls.append(args) or 0,
        )
        with self.assertRaises(worker_pidfd.UnsupportedPidfdError):
            unsupported_arch.send_signal(123, signal.SIGTERM)
        self.assertEqual(
            calls, [], "unsupported architecture must fail before any syscall"
        )

        enosys = worker_pidfd.PidfdOperations(
            system="Linux",
            machine="aarch64",
            syscall_fn=lambda *_args: (_ for _ in ()).throw(
                OSError(errno.ENOSYS, "no syscall")
            ),
        )
        with self.assertRaises(worker_pidfd.UnsupportedPidfdError):
            enosys.send_signal(123, signal.SIGTERM)
        denied = worker_pidfd.PidfdOperations(
            system="Linux",
            machine="aarch64",
            syscall_fn=lambda *_args: (_ for _ in ()).throw(
                PermissionError(errno.EPERM, "denied")
            ),
        )
        with self.assertRaises(PermissionError):
            denied.send_signal(123, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
