from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.maintenance.phase39_p0b import release_entrypoint, release_manager


CONFIG_FINGERPRINT = "c" * 64
SOURCE_MANIFEST_SHA256 = "d" * 64
WORKSHEET_SHA256 = "e" * 64
NOW = dt.datetime(2026, 10, 20, 13, 0, tzinfo=dt.timezone.utc)


class ReleaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("root-owned release fixture requires root test process")
        self.temp = tempfile.TemporaryDirectory(prefix="phase39-p0b-release-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.payload = self.base / "payload"
        self.payload.mkdir()
        self.launcher = {
            "launcher": "start.sh:start_worker_only",
            "executable": "/usr/bin/python3.12",
            "script": "start.sh",
            "cwd": "/usr/local/lib/orchestrator-maintenance/phase39-p0b/r-fixture",
            "argv": [
                "<release>/venv/bin/python3",
                "<release>/venv/bin/celery",
                "-A",
                "app.celery_app",
                "worker",
                "--concurrency=4",
                "--loglevel=info",
            ],
        }
        self._write(
            "scripts/maintenance/phase39_p0b/operator_tooling.py",
            b"# operator\n",
            executable=True,
        )
        self._write("scripts/maintenance/phase39_p0b/worker_pidfd.py", b"# pidfd\n")
        self._write(
            "scripts/maintenance/phase39_p0b/protected_refresh_authority.py",
            b"# authority\n",
        )
        self._write(
            "scripts/maintenance/phase39_p0b/release_manager.py", b"# manager\n"
        )
        self._write(
            "scripts/maintenance/phase39_p0b/release_entrypoint.py", b"# entry\n"
        )
        self._write(
            "scripts/maintenance/phase39_p0b/release_install_entrypoint.py",
            b"# installer\n",
        )
        self._write(
            "scripts/maintenance/phase39_p0b/entrypoint.sh",
            b"#!/bin/bash\n",
            executable=True,
        )
        self._write(
            "scripts/maintenance/phase39_p0b/release-install.sh",
            b"#!/bin/bash\n",
            executable=True,
        )
        self._write(
            "venv/bin/python3",
            Path("/usr/bin/python3.12").read_bytes(),
            executable=True,
        )
        self._write(
            "venv/bin/celery",
            b"#!/usr/local/lib/orchestrator-maintenance/phase39-p0b/r-fixture/venv/bin/python3\n",
            executable=True,
        )
        self._write(
            "venv/pyvenv.cfg",
            b"home = /usr/bin\ninclude-system-site-packages = false\n",
        )
        self._write("start.sh", b"#!/bin/bash\n", executable=True)
        self._write(
            "scripts/maintenance/worker_pin_worksheet.py", b"# worksheet helper\n"
        )
        self._write("app/services/orchestration/worker_identity.py", b"# identity\n")
        self._write(
            "app/services/orchestration/invocation_provenance.py", b"# provenance\n"
        )
        self._write("app/config.py", b"# app config\n")
        self._write(
            "venv/lib/python3.12/site-packages/celery/__init__.py", b"# dependency\n"
        )
        self._write(
            "venv/lib/python3.12/stdlib/encodings/__init__.py",
            b"# stdlib inventory fixture\n",
        )
        self._write(
            "venv/lib/dependency-stub", b"protected external dependency fixture\n"
        )
        self._write(".env", b"CELERY_WORKER_CONCURRENCY=4\n", mode=0o600)
        self.source_tree = release_manager._git_tree_id(self.payload)
        self.commit_object = (
            f"tree {self.source_tree}\n"
            "author Phase39 Test <phase39@example.invalid> 1792470000 +0000\n"
            "committer Phase39 Test <phase39@example.invalid> 1792470000 +0000\n\n"
            "temporary release fixture\n"
        ).encode()
        self.source_head = hashlib.sha1(
            b"commit "
            + str(len(self.commit_object)).encode()
            + b"\0"
            + self.commit_object
        ).hexdigest()
        self.commit_object_b64 = base64.b64encode(self.commit_object).decode("ascii")
        self.files = release_manager._walk_release_files(self.payload)
        self.dependencies = self._dependencies()
        self.manifest, self.manifest_sha = release_manager.generate_source_manifest(
            self.payload,
            release_id="r-fixture",
            source_head=self.source_head,
            source_tree=self.source_tree,
            commit_object_b64=self.commit_object_b64,
            source_manifest_sha256=SOURCE_MANIFEST_SHA256,
            launcher_identity=self.launcher,
            dependencies=self.dependencies,
            configuration_fingerprint=CONFIG_FINGERPRINT,
        )
        self.manifest_path = self.base / "release-manifest.json"
        release_manager.write_source_manifest(self.manifest, self.manifest_path)
        os.chmod(self.manifest_path, 0o644)
        self.parent = self.base / "usr/local/lib/orchestrator-maintenance/phase39-p0b"
        self.parent.mkdir(parents=True, mode=0o755)
        for current, dirs, _ in os.walk(self.base):
            for name in dirs:
                os.chmod(Path(current) / name, 0o755)

    def _write(
        self,
        relative: str,
        contents: bytes,
        *,
        executable: bool = False,
        mode: int | None = None,
    ) -> Path:
        path = self.payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        os.chmod(path, mode if mode is not None else (0o755 if executable else 0o644))
        return path

    def _dependencies(self) -> list[dict[str, str]]:
        files = release_manager._walk_release_files(self.payload)
        table = {entry["path"]: entry for entry in files}
        stub = table["venv/lib/dependency-stub"]["sha256"]
        module_paths = {
            "operator_tooling": "scripts/maintenance/phase39_p0b/operator_tooling.py",
            "worker_pidfd": "scripts/maintenance/phase39_p0b/worker_pidfd.py",
            "protected_refresh_authority": "scripts/maintenance/phase39_p0b/protected_refresh_authority.py",
            "release_manager": "scripts/maintenance/phase39_p0b/release_manager.py",
            "release_entrypoint": "scripts/maintenance/phase39_p0b/release_entrypoint.py",
            "operator_entrypoint_wrapper": "scripts/maintenance/phase39_p0b/entrypoint.sh",
            "release_install_entrypoint": "scripts/maintenance/phase39_p0b/release_install_entrypoint.py",
            "release_install_wrapper": "scripts/maintenance/phase39_p0b/release-install.sh",
            "start_sh": "start.sh",
            "worker_pin_worksheet": "scripts/maintenance/worker_pin_worksheet.py",
            "worker_identity": "app/services/orchestration/worker_identity.py",
            "invocation_provenance": "app/services/orchestration/invocation_provenance.py",
            "python_interpreter": "venv/bin/python3",
            "venv_python": "venv/bin/python3",
            "celery_cli": "venv/bin/celery",
            "venv_configuration": "venv/pyvenv.cfg",
            "worker_configuration": ".env",
            "python_stdlib": "venv/lib/python3.12/stdlib/encodings/__init__.py",
            "bash": "venv/lib/dependency-stub",
            "env": "venv/lib/dependency-stub",
            "nohup": "venv/lib/dependency-stub",
            "cmp": "venv/lib/dependency-stub",
            "stat": "venv/lib/dependency-stub",
            "openssl": "venv/lib/dependency-stub",
            "dynamic_loader": "venv/lib/dependency-stub",
            "libc": "venv/lib/dependency-stub",
            "libpython": "venv/lib/dependency-stub",
        }
        result: list[dict[str, str]] = []
        for name, path in module_paths.items():
            result.append(
                {
                    "name": name,
                    "kind": "release-file",
                    "path": path,
                    "sha256": table[path]["sha256"],
                    "version": "fixture",
                }
            )
        app_files = [entry for entry in files if entry["path"].startswith("app/")]
        runtime_files = [
            entry
            for entry in files
            if entry["path"].startswith("venv/lib/python3.12/site-packages/")
        ]
        stdlib_files = [
            entry
            for entry in files
            if entry["path"].startswith("venv/lib/python3.12/stdlib/")
        ]
        result.extend(
            [
                {
                    "name": "application_source",
                    "kind": "release-tree",
                    "path": "app",
                    "sha256": release_manager._file_table_digest(app_files),
                    "version": "fixture",
                },
                {
                    "name": "python_dependencies",
                    "kind": "release-tree",
                    "path": "venv/lib/python3.12/site-packages",
                    "sha256": release_manager._file_table_digest(runtime_files),
                    "version": "fixture",
                },
                {
                    "name": "python_stdlib_inventory",
                    "kind": "release-tree",
                    "path": "venv/lib/python3.12/stdlib",
                    "sha256": release_manager._file_table_digest(stdlib_files),
                    "version": "fixture",
                },
                {
                    "name": "kernel_pidfd",
                    "kind": "runtime-interface",
                    "path": "linux:pidfd_open,pidfd_send_signal",
                    "sha256": hashlib.sha256(
                        b"linux:pidfd_open,pidfd_send_signal"
                    ).hexdigest(),
                    "version": "fixture",
                },
                {
                    "name": "procfs",
                    "kind": "runtime-interface",
                    "path": "linux:/proc",
                    "sha256": hashlib.sha256(b"linux:/proc").hexdigest(),
                    "version": "fixture",
                },
            ]
        )
        return result

    def install(self, **kwargs):
        return release_manager.install_release(
            self.payload,
            self.manifest_path,
            self.parent,
            expected_manifest_sha256=self.manifest_sha,
            expected_source_head=self.source_head,
            expected_source_tree=self.source_tree,
            expected_launcher_identity=self.launcher,
            **kwargs,
        )

    @property
    def release(self) -> Path:
        return self.parent / "r-fixture"

    def require_valid(self) -> dict:
        return release_manager.verify_installed_release(
            self.release,
            expected_manifest_sha256=self.manifest_sha,
            expected_source_head=self.source_head,
            expected_source_tree=self.source_tree,
            expected_launcher_identity=self.launcher,
        )


class ProtectedReleaseTests(ReleaseFixture):
    def test_correct_protected_release_fixture_installs_and_verifies(self) -> None:
        result = self.install()
        self.assertEqual(result["installation"], "ATOMIC_INSTALL_VERIFIED")
        self.assertEqual(result["activation"], "BLOCKED_PENDING_APPROVAL")
        self.assertEqual(result["status"], "RELEASE_INSTALLED_NOT_APPROVED")
        self.assertEqual(stat.S_IMODE(self.release.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((self.release / ".env").stat().st_mode), 0o600)
        self.assertEqual(self.require_valid()["release_id"], "r-fixture")

    def test_install_requires_explicit_manifest_digest(self) -> None:
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "expected_manifest_digest_required"
        ):
            release_manager.install_release(
                self.payload,
                self.manifest_path,
                self.parent,
                expected_manifest_sha256="",
                expected_source_head=self.source_head,
                expected_source_tree=self.source_tree,
                expected_launcher_identity=self.launcher,
            )

    def test_production_install_destination_is_fixed(self) -> None:
        self.assertEqual(
            release_manager.validate_protected_installation_parent(
                "/usr/local/lib/orchestrator-maintenance/phase39-p0b"
            ),
            Path("/usr/local/lib/orchestrator-maintenance/phase39-p0b"),
        )
        with self.assertRaisesRegex(
            release_manager.ReleaseError,
            "installation_parent_must_be_fixed_protected_release_root",
        ):
            release_manager.validate_protected_installation_parent(self.parent)

    def test_incomplete_runtime_dependency_inventory_is_rejected(self) -> None:
        incomplete = [
            item for item in self.dependencies if item["name"] != "python_stdlib"
        ]
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "runtime_dependency_inventory_incomplete"
        ):
            release_manager.generate_source_manifest(
                self.payload,
                release_id="missing-dependency",
                source_head=self.source_head,
                source_tree=self.source_tree,
                commit_object_b64=self.commit_object_b64,
                source_manifest_sha256=SOURCE_MANIFEST_SHA256,
                launcher_identity=self.launcher,
                dependencies=incomplete,
                configuration_fingerprint=CONFIG_FINGERPRINT,
            )

    def test_python_dependency_pth_startup_code_is_rejected(self) -> None:
        self._write(
            "venv/lib/python3.12/site-packages/untrusted.pth",
            b"import pathlib; pathlib.Path('/tmp/phase39-p0b-pth-ran').touch()\n",
        )
        dependencies = self._dependencies()
        with self.assertRaisesRegex(
            release_manager.ReleaseError,
            "executable_python_path_configuration_rejected",
        ):
            release_manager._validate_dependencies(
                dependencies, release_manager._walk_release_files(self.payload)
            )

    def test_group_or_world_writable_system_dependency_file_is_rejected(self) -> None:
        system_tree = self.base / "system-python-fixture"
        system_tree.mkdir(mode=0o755)
        dependency_file = system_tree / "module.py"
        dependency_file.write_text("value = 1\n", encoding="utf-8")
        os.chmod(dependency_file, 0o666)
        dependency = {
            "name": "python_stdlib",
            "kind": "system-tree",
            "path": str(system_tree),
            "sha256": "0" * 64,
            "version": "fixture",
        }
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "system_dependency_file_unprotected"
        ):
            release_manager._verify_system_dependency(dependency, uid=0)

    def test_source_path_replacement_after_open_cannot_substitute_release_bytes(
        self,
    ) -> None:
        source = self.payload / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        expected = source.read_bytes()
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            fd = real_open(path, flags, *args, **kwargs)
            if (
                not replaced
                and Path(path) == source
                and flags & os.O_ACCMODE == os.O_RDONLY
            ):
                replaced = True
                source.rename(source.with_name("worker_pidfd.original"))
                replacement_fd = real_open(
                    source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
                )
                try:
                    os.write(
                        replacement_fd, b"substituted after source descriptor opened\n"
                    )
                finally:
                    os.close(replacement_fd)
            return fd

        with patch.object(release_manager.os, "open", side_effect=replacing_open):
            self.install()
        self.assertTrue(replaced)
        installed = self.release / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        self.assertEqual(installed.read_bytes(), expected)
        self.assertEqual(self.require_valid()["release_id"], "r-fixture")

    def test_source_change_after_hash_inventory_is_rejected(self) -> None:
        self._write(
            "scripts/maintenance/phase39_p0b/worker_pidfd.py",
            b"substituted after inventory\n",
        )
        with self.assertRaisesRegex(
            release_manager.ReleaseError,
            "runtime_dependency_missing_or_changed|source_changed_after_manifest",
        ):
            self.install()
        self.assertFalse(self.release.exists())

    def test_world_writable_operator_script_is_rejected(self) -> None:
        self.install()
        path = self.release / "scripts/maintenance/phase39_p0b/operator_tooling.py"
        os.chmod(path, 0o466)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_file_unprotected_or_changed"
        ):
            self.require_valid()

    def test_group_writable_imported_helper_is_rejected(self) -> None:
        self.install()
        path = self.release / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        os.chmod(path, 0o464)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_file_unprotected_or_changed"
        ):
            self.require_valid()

    def test_writable_parent_directory_is_rejected(self) -> None:
        self.install()
        unsafe = self.parent.parent
        original = stat.S_IMODE(unsafe.stat().st_mode)
        try:
            os.chmod(unsafe, 0o775)
            with self.assertRaisesRegex(
                release_manager.ReleaseError, "directory_writable_by_untrusted_user"
            ):
                self.require_valid()
        finally:
            os.chmod(unsafe, original)

    def test_writable_start_script_is_rejected(self) -> None:
        self.install()
        os.chmod(self.release / "start.sh", 0o755)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_file_unprotected_or_changed"
        ):
            self.require_valid()

    def test_writable_application_module_is_rejected(self) -> None:
        self.install()
        os.chmod(self.release / "app/config.py", 0o644)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_file_unprotected_or_changed"
        ):
            self.require_valid()

    def test_symlink_substitution_is_rejected(self) -> None:
        self.install()
        target = self.release / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        target.unlink()
        target.symlink_to("protected_refresh_authority.py")
        with self.assertRaises(release_manager.ReleaseError):
            self.require_valid()

    def test_hardlink_substitution_is_rejected(self) -> None:
        self.install()
        target = self.release / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        source = (
            self.release
            / "scripts/maintenance/phase39_p0b/protected_refresh_authority.py"
        )
        target.unlink()
        os.link(source, target)
        with self.assertRaisesRegex(
            release_manager.ReleaseError,
            "file_type_or_link_count_invalid|release_file_unprotected_or_changed",
        ):
            self.require_valid()

    def test_dependency_replacement_after_hash_check_is_denied_to_unprivileged_user(
        self,
    ) -> None:
        self.install()
        self.require_valid()
        os.chmod(self.base, 0o755)
        target = self.release / "scripts/maintenance/phase39_p0b/worker_pidfd.py"
        child_code = (
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('replacement')"
        )
        completed = subprocess.run(
            ["/usr/bin/python3.12", "-I", "-c", child_code, str(target)],
            cwd="/",
            env={"HOME": "/tmp", "PATH": "/usr/bin:/bin"},
            preexec_fn=lambda: (os.setgid(1000), os.setuid(1000)),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PermissionError", completed.stderr)
        self.assertEqual(self.require_valid()["release_id"], "r-fixture")

    def test_incomplete_release_manifest_is_rejected(self) -> None:
        document = dict(self.manifest)
        document.pop("runtime_dependencies")
        bad_path = self.base / "incomplete-manifest.json"
        bad_path.write_bytes(release_manager.canonical_json(document))
        os.chmod(bad_path, 0o644)
        digest = hashlib.sha256(bad_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "manifest_fields_incomplete_or_unknown"
        ):
            release_manager.install_release(
                self.payload,
                bad_path,
                self.parent,
                expected_manifest_sha256=digest,
                expected_source_head=self.source_head,
                expected_source_tree=self.source_tree,
                expected_launcher_identity=self.launcher,
            )

    def test_changed_release_file_hash_is_rejected(self) -> None:
        self.install()
        path = self.release / "app/config.py"
        os.chmod(path, 0o644)
        path.write_text("changed\n", encoding="utf-8")
        os.chmod(path, 0o444)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_file_unprotected_or_changed"
        ):
            self.require_valid()

    def test_wrong_source_head_or_tree_is_rejected(self) -> None:
        self.install()
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "source_head_or_tree_mismatch"
        ):
            release_manager.verify_installed_release(
                self.release,
                expected_manifest_sha256=self.manifest_sha,
                expected_source_head="a" * 40,
                expected_source_tree=self.source_tree,
                expected_launcher_identity=self.launcher,
            )

    def test_wrong_launcher_identity_is_rejected(self) -> None:
        self.install()
        changed = dict(self.launcher, launcher="start.sh:start_workers")
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "launcher_identity_mismatch"
        ):
            release_manager.verify_installed_release(
                self.release,
                expected_manifest_sha256=self.manifest_sha,
                expected_source_head=self.source_head,
                expected_source_tree=self.source_tree,
                expected_launcher_identity=changed,
            )

    def test_historical_approval_is_rejected(self) -> None:
        approval = self._approval("P39-P0B-GATEB-002")
        with self.assertRaisesRegex(
            release_entrypoint.EntryPointError, "historical_approval_invalid"
        ):
            release_entrypoint.validate_operator_approval(
                approval,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                now=NOW,
            )

    def test_protected_entrypoint_rejects_pin_and_source_path_overrides(self) -> None:
        for command, arguments in (
            ("install", ["--pin-dir=/tmp/other"]),
            ("install", ["--pin-d", "/tmp/other"]),
            ("install", ["--worksheet", "/tmp/worksheet.json"]),
            ("refresh", ["--pin-path", "/tmp/other-pin"]),
            ("gate-a", ["--source-root=/tmp/checkout"]),
            ("gate-a", ["--runtime-root", "/tmp/runtime"]),
        ):
            with self.subTest(command=command, arguments=arguments):
                with self.assertRaisesRegex(
                    release_entrypoint.EntryPointError,
                    "operator_source_runtime_worksheet_or_pin_override_rejected",
                ):
                    release_entrypoint._validate_operator_arguments(command, arguments)
        release_entrypoint._validate_operator_arguments(
            "refresh", ["--master-pid", "123"]
        )

    def test_approval_binds_source_release_dependencies_worksheet_and_validity(
        self,
    ) -> None:
        approval = self._approval("P39-P0B-RELEASE-TEST-001")
        validated = release_entrypoint.validate_operator_approval(
            approval, manifest=self.manifest, manifest_sha256=self.manifest_sha, now=NOW
        )
        self.assertEqual(validated["release_manifest_sha256"], self.manifest_sha)
        changed = dict(approval, runtime_dependency_inventory_sha256="f" * 64)
        with self.assertRaisesRegex(
            release_entrypoint.EntryPointError, "approval_release_binding_mismatch"
        ):
            release_entrypoint.validate_operator_approval(
                changed,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                now=NOW,
            )

    def test_approval_rejects_boolean_schema_and_duplicate_module_identity(
        self,
    ) -> None:
        approval = self._approval("P39-P0B-RELEASE-TEST-001")
        approval["schema_version"] = True
        with self.assertRaisesRegex(
            release_entrypoint.EntryPointError, "approval_schema_unsupported"
        ):
            release_entrypoint.validate_operator_approval(
                approval,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                now=NOW,
            )
        approval = self._approval("P39-P0B-RELEASE-TEST-001")
        approval["identity_policy"]["affected_module_names"] = [
            "app.config",
            "app.config",
        ]
        approval["identity_policy"]["affected_module_count"] = 2
        with self.assertRaisesRegex(
            release_entrypoint.EntryPointError, "approval_identity_policy_invalid"
        ):
            release_entrypoint.validate_operator_approval(
                approval,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                now=NOW,
            )

    def _approval(self, approval_id: str) -> dict:
        return {
            "approval_kind": "orchestrator-phase39-p0b-operator-approval",
            "schema_version": 1,
            "approval_id": approval_id,
            "operator_identity": "test-operator",
            "valid_from": "2026-10-20T12:00:00Z",
            "valid_until": "2026-10-20T14:00:00Z",
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "release_id": self.manifest["release_id"],
            "release_manifest_sha256": self.manifest_sha,
            "launcher_identity": self.launcher,
            "runtime_dependency_inventory_sha256": self.manifest[
                "runtime_dependency_inventory_sha256"
            ],
            "source_manifest_sha256": self.manifest["source_manifest_sha256"],
            "configuration_fingerprint": self.manifest["configuration_fingerprint"],
            "worksheet_sha256": WORKSHEET_SHA256,
            "identity_policy": {
                "configuration_schema_version": 2,
                "configuration_policy_id": "phase37-worker-boot-test-v2",
                "worker_node_identity": "celery@test",
                "affected_module_count": 1,
                "affected_module_names": ["app.config"],
            },
        }

    def test_unsafe_release_is_rejected_before_action_callback(self) -> None:
        self.install()
        path = self.release / "app/config.py"
        os.chmod(path, 0o644)
        actions: list[str] = []
        with self.assertRaises(release_manager.ReleaseError):
            release_entrypoint.invoke_after_release_verification(
                self.release,
                expected_manifest_sha256=self.manifest_sha,
                expected_source_head=self.source_head,
                expected_source_tree=self.source_tree,
                expected_launcher_identity=self.launcher,
                action=lambda _: actions.append("privileged-action"),
            )
        self.assertEqual(actions, [])

    def test_concurrent_installation_is_serialized(self) -> None:
        ready = threading.Event()
        resume = threading.Event()
        outcomes: list[object] = []

        def hold_after_stage(stage_name: str) -> None:
            if stage_name == "after_stage":
                ready.set()
                resume.wait(5)

        def first_install() -> None:
            try:
                outcomes.append(self.install(fault_hook=hold_after_stage))
            except BaseException as exc:  # surfaced in the parent assertion
                outcomes.append(exc)

        thread = threading.Thread(target=first_install)
        thread.start()
        self.assertTrue(ready.wait(5))
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "concurrent_or_interrupted_installation_lock"
        ):
            self.install()
        resume.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertNotIsInstance(outcomes[0], BaseException)
        self.assertEqual(self.require_valid()["release_id"], "r-fixture")

    def test_interrupted_pre_rename_install_cleans_private_stage(self) -> None:
        def interrupt(stage_name: str) -> None:
            if stage_name == "after_stage":
                raise RuntimeError("fixture interruption")

        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            self.install(fault_hook=interrupt)
        self.assertFalse(self.release.exists())
        self.assertEqual(list(self.parent.glob(".*.staging-*")), [])
        self.assertEqual(list(self.parent.glob("*.lock")), [])

    def test_interrupted_post_rename_keeps_explicit_recovery_state(self) -> None:
        def interrupt(stage_name: str) -> None:
            if stage_name == "after_rename":
                raise RuntimeError("fixture interruption")

        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            self.install(fault_hook=interrupt)
        self.assertTrue(self.release.is_dir())
        self.assertEqual(
            self.require_valid()["status"], "RELEASE_INSTALLED_NOT_APPROVED"
        )
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_destination_exists_or_partial"
        ):
            self.install()

    def test_partial_release_directory_is_not_replaced(self) -> None:
        self.release.mkdir(mode=0o700)
        with self.assertRaisesRegex(
            release_manager.ReleaseError, "release_destination_exists_or_partial"
        ):
            self.install()
        with self.assertRaises(release_manager.ReleaseError):
            self.require_valid()

    def test_untrusted_pythonpath_and_sitecustomize_are_ignored_by_isolated_mode(
        self,
    ) -> None:
        hostile = self.base / "hostile-pythonpath"
        hostile.mkdir()
        marker = self.base / "sitecustomize-ran"
        (hostile / "sitecustomize.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "/usr/bin/python3.12",
                "-I",
                "-S",
                "-c",
                "import json,sys; print(json.__file__)",
            ],
            cwd=str(hostile),
            env={"PYTHONPATH": str(hostile), "HOME": "/tmp", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(str(hostile), result.stdout)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
