from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.maintenance.phase39_p0b import release_packaging as packaging


HEAD = "a" * 40
TREE = "b" * 40
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object, *, ascii_only: bool = False) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ascii_only,
        allow_nan=False,
    ).encode("utf-8")


def _source_manifest(source_bytes: bytes) -> tuple[bytes, str]:
    payload = {
        "schema_version": 1,
        "candidate_head": HEAD,
        "candidate_tree": TREE,
        "source_root": "/build/source",
        "dirty_source_policy": "tracked-and-untracked-source-must-be-clean",
        "affected_modules": ["app.tasks.worker"],
        "resolved_modules": [
            {
                "module_name": "app.tasks.worker",
                "expected_source_path": "/build/source/app/tasks/worker.py",
                "source_sha256": _sha(source_bytes),
            }
        ],
        "launcher_identity": {
            "launcher": "celery-worker",
            "executable": "/opt/orchestrator/worker-releases/build/runtime/bin/python3",
            "cwd": "/build/source",
            "argv": ["celery", "-A", "app.celery_app", "worker"],
        },
    }
    identity_digest = _sha(_canonical(payload, ascii_only=True))
    value = dict(payload, source_manifest_digest=identity_digest)
    return _canonical(value, ascii_only=True), identity_digest


def _chmod_tree(root: Path, *, writable: bool) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                continue
            executable = path.name in {"worker-bootstrap.py", "python3", "celery"}
            path.chmod(0o644 if writable else (0o555 if executable else 0o444))
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o755 if writable else 0o555)
    root.chmod(0o755 if writable else 0o555)


class WorkerReleaseFixture:
    def __init__(self, *, signed: bool = False) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="worker-release-test-")
        self.parent = Path(self.temp.name)
        self.stage = self.parent / "candidate-stage"
        self.stage.mkdir()
        source_bytes = b"def task():\n    return 'synthetic'\n"
        files = {
            "launcher/worker-bootstrap.py": b"#!/usr/bin/python3\n# synthetic launcher\n",
            "runtime/bin/python3": b"synthetic copied python runtime\n",
            "runtime/bin/celery": b"#!/opt/release/runtime/bin/python3\n# synthetic celery\n",
            "source/app/tasks/worker.py": source_bytes,
            "provenance/runtime-lock.json": b'{"fixture":"lock"}',
            "provenance/wheel-inventory.json": b'{"fixture":"wheels"}',
            "provenance/native-extension-inventory.json": b'{"fixture":"native"}',
            "provenance/elf-closure.json": b'{"fixture":"elf"}',
            "provenance/import-origins.json": b'{"fixture":"imports"}',
        }
        for relative, content in files.items():
            path = self.stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        source_raw, source_identity_digest = _source_manifest(source_bytes)
        (self.stage / "provenance/source-manifest.json").write_bytes(source_raw)
        if signed:
            (self.stage / packaging.WORKER_RELEASE_SIGNATURE).write_bytes(
                b"not-a-valid-or-recognized-signature"
            )
        _chmod_tree(self.stage, writable=False)
        root_fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            inventory = packaging._worker_inventory_release(root_fd)
        finally:
            os.close(root_fd)
        refs = {}
        digest_fields = {}
        role_paths = {
            "source_manifest": "provenance/source-manifest.json",
            "runtime_lock": "provenance/runtime-lock.json",
            "wheel_inventory": "provenance/wheel-inventory.json",
            "native_extension_inventory": "provenance/native-extension-inventory.json",
            "elf_closure": "provenance/elf-closure.json",
            "import_origin_inventory": "provenance/import-origins.json",
        }
        for role, relative in role_paths.items():
            content = (self.stage / relative).read_bytes()
            digest = _sha(content)
            refs[role] = {"path": relative, "sha256": digest}
            if role != "source_manifest":
                digest_fields[
                    {
                        "runtime_lock": "runtime_lock_digest",
                        "wheel_inventory": "wheel_inventory_digest",
                        "native_extension_inventory": "native_extension_inventory_digest",
                        "elf_closure": "elf_closure_digest",
                        "import_origin_inventory": "import_origin_inventory_digest",
                    }[role]
                ] = digest
        core = {
            "manifest_kind": packaging.WORKER_RELEASE_KIND,
            "schema_version": 2,
            "source": {
                "head": HEAD,
                "tree": TREE,
                "source_manifest_digest": source_identity_digest,
            },
            "payload_inventory": inventory,
            "platform_target": {
                "distribution": "Ubuntu",
                "release": "24.04",
                "architecture": "aarch64",
                "libc_baseline": "2.39",
            },
            "python_runtime": {
                "implementation": "CPython",
                "version": "3.12.3",
                "abi": "cp312",
                "soabi": "cpython-312-aarch64-linux-gnu",
            },
            **digest_fields,
            "launcher": {
                "path": "launcher/worker-bootstrap.py",
                "sha256": _sha(files["launcher/worker-bootstrap.py"]),
            },
            "interpreter": {
                "path": "runtime/bin/python3",
                "sha256": _sha(files["runtime/bin/python3"]),
                "link_policy": "copied-regular-file",
                "symlink_chain": [],
            },
            "celery_entrypoint": {
                "path": "runtime/bin/celery",
                "sha256": _sha(files["runtime/bin/celery"]),
            },
            "configuration": {
                "policy_id": "worker-config-projection/v3",
                "profile_reference": {
                    "path": "/opt/orchestrator/worker-config/test/profile.json",
                    "sha256": _sha(b"approved-profile-reference"),
                },
            },
            "state_path_roles": {
                role: f"state-role:{role}"
                for role in sorted(packaging.WORKER_RELEASE_STATE_ROLES)
            },
            "build_provenance": {
                "builder_id": "synthetic-test-fixture",
                "recipe_digest": _sha(b"recipe"),
                "toolchain_digest": _sha(b"toolchain"),
                "built_at_utc": "2026-10-21T00:00:00Z",
                "references": refs,
            },
        }
        self.manifest = packaging.build_worker_release_manifest(core)
        self.stage.chmod(0o755)
        (self.stage / packaging.WORKER_RELEASE_MANIFEST).write_bytes(
            packaging._worker_canonical_json(self.manifest)
        )
        (self.stage / packaging.WORKER_RELEASE_MANIFEST).chmod(0o444)
        self.stage.chmod(0o555)
        self.release = self.parent / self.manifest["release_id"]
        self.stage.rename(self.release)

    def close(self) -> None:
        _chmod_tree(self.release, writable=True)
        self.temp.cleanup()


class WorkerReleaseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkerReleaseFixture()
        self.addCleanup(self.fixture.close)

    def _rewrite_manifest(self, manifest: dict[str, object]) -> None:
        root = self.fixture.release
        root.chmod(0o755)
        path = root / packaging.WORKER_RELEASE_MANIFEST
        path.chmod(0o644)
        path.write_bytes(packaging._worker_canonical_json(manifest))
        path.chmod(0o444)
        root.chmod(0o555)

    def _add_object(self, name: str, *, kind: str = "file", target: str = "x") -> Path:
        root = self.fixture.release
        root.chmod(0o755)
        path = root / name
        if kind == "symlink":
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"extra")
            path.chmod(0o444)
        root.chmod(0o555)
        return path

    def test_canonical_identity_is_deterministic(self) -> None:
        self.assertEqual(
            packaging._worker_canonical_json({"b": 2, "a": [1, "x"]}),
            b'{"a":[1,"x"],"b":2}',
        )
        self.assertEqual(
            packaging.build_worker_release_manifest(
                packaging._worker_identity_core(self.fixture.manifest)
            ),
            self.fixture.manifest,
        )

    def test_field_order_invariance(self) -> None:
        first = {"outer": {"z": 1, "a": 2}, "items": [3, 4]}
        second = {"items": [3, 4], "outer": {"a": 2, "z": 1}}
        self.assertEqual(
            packaging._worker_canonical_json(first),
            packaging._worker_canonical_json(second),
        )

    def test_unknown_and_missing_fields_rejected(self) -> None:
        unknown = dict(self.fixture.manifest, surprise=True)
        with self.assertRaisesRegex(packaging.PackagingError, "unknown_fields"):
            packaging._validate_worker_release_manifest(unknown)
        missing = dict(self.fixture.manifest)
        missing.pop("launcher")
        with self.assertRaisesRegex(packaging.PackagingError, "missing_fields"):
            packaging._validate_worker_release_manifest(missing)
        unsupported = dict(self.fixture.manifest, schema_version=3)
        with self.assertRaisesRegex(packaging.PackagingError, "schema_unsupported"):
            packaging._validate_worker_release_manifest(unsupported)

    def test_digest_mismatch_rejected(self) -> None:
        changed = dict(self.fixture.manifest)
        changed["execution_artifact_digest"] = "0" * 64
        with self.assertRaisesRegex(
            packaging.PackagingError, "artifact_digest_mismatch"
        ):
            packaging._validate_worker_release_manifest(changed)

    def test_payload_mutation_rejected(self) -> None:
        path = self.fixture.release / "source/app/tasks/worker.py"
        path.chmod(0o644)
        path.write_bytes(b"mutated task source\n")
        path.chmod(0o444)
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("payload_inventory_mismatch", result["error"])

    def test_extra_undeclared_file_rejected(self) -> None:
        self._add_object("unexpected.txt")
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("payload_inventory_mismatch", result["error"])

    def test_duplicate_inventory_path_rejected(self) -> None:
        changed = dict(self.fixture.manifest)
        changed["payload_inventory"] = list(changed["payload_inventory"]) + [
            dict(changed["payload_inventory"][0])
        ]
        with self.assertRaisesRegex(
            packaging.PackagingError, "duplicate_inventory_path"
        ):
            packaging._validate_worker_release_manifest(changed)

    def test_absolute_and_parent_paths_rejected(self) -> None:
        for path in ("/etc/passwd", "../escape", "source/../../escape"):
            with self.subTest(path=path), self.assertRaises(packaging.PackagingError):
                packaging._safe_worker_path(path)

    def test_symlink_escape_rejected(self) -> None:
        self._add_object("escape", kind="symlink", target="/etc/passwd")
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("symlink_escape", result["error"])

    def test_broken_symlink_rejected(self) -> None:
        self._add_object("broken", kind="symlink", target="missing")
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("symlink_broken", result["error"])

    def test_symlink_loop_rejected(self) -> None:
        root = self.fixture.release
        root.chmod(0o755)
        (root / "loop-a").symlink_to("loop-b")
        (root / "loop-b").symlink_to("loop-a")
        root.chmod(0o555)
        result = packaging.verify_worker_release(root)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("symlink_loop", result["error"])

    def test_undeclared_internal_symlink_rejected(self) -> None:
        self._add_object("alias", kind="symlink", target="runtime/bin/python3")
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("payload_inventory_mismatch", result["error"])

    def test_hard_link_rejected(self) -> None:
        root = self.fixture.release
        root.chmod(0o755)
        os.link(root / "runtime/bin/python3", root / "hard-linked-python")
        root.chmod(0o555)
        result = packaging.verify_worker_release(root)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("hard_link_rejected", result["error"])

    def test_special_file_rejected(self) -> None:
        self._add_object("queue", kind="fifo")
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("special_file_rejected", result["error"])

    def test_source_head_tree_mismatch_rejected(self) -> None:
        result = packaging.verify_worker_release(
            self.fixture.release, expected_source_head="c" * 40
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("expected_source_head_mismatch", result["error"])

    def test_signature_mismatch_fails_closed_without_activation(self) -> None:
        signed = WorkerReleaseFixture(signed=True)
        self.addCleanup(signed.close)
        result = packaging.verify_worker_release(signed.release)
        self.assertEqual(
            result["status"], "INTEGRITY_VERIFIED_SIGNATURE_CONTRACT_UNSUPPORTED"
        )
        self.assertEqual(result["signature_status"], "PRESENT_UNVERIFIED")
        self.assertFalse(result["activation_eligible"])

    def test_unsigned_candidate_rejected_for_activation(self) -> None:
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "INTEGRITY_VERIFIED_UNSIGNED_REJECTED")
        self.assertFalse(result["activation_eligible"])

    def test_digest_excludes_self_identity_and_signature(self) -> None:
        core = packaging._worker_identity_core(self.fixture.manifest)
        self.assertNotIn("execution_artifact_digest", core)
        self.assertNotIn("release_id", core)
        with self.assertRaisesRegex(packaging.PackagingError, "self_identity_input"):
            packaging.build_worker_release_manifest(self.fixture.manifest)

    def test_writable_release_root_rejected(self) -> None:
        self.fixture.release.chmod(0o755)
        result = packaging.verify_worker_release(self.fixture.release)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("root_writable", result["error"])

    def test_no_runtime_or_network_side_effects(self) -> None:
        with mock.patch.object(
            socket, "socket", side_effect=AssertionError("network access attempted")
        ), mock.patch.object(
            subprocess, "run", side_effect=AssertionError("subprocess attempted")
        ):
            result = packaging.verify_worker_release(self.fixture.release)
        self.assertTrue(result["integrity_verified"])
        self.assertFalse(result["activation_eligible"])
        self.assertFalse(result["runtime_closure_complete"])


if __name__ == "__main__":
    unittest.main()
